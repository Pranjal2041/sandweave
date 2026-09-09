"""Guest-only command/file service. Processes own guest files, not host exec pipes."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import pwd
import re
import signal
import socket
import subprocess
import sys
import threading
import time

try:
    from sandweave.sandbox.wire import decode, encode, MAX_BODY
except ImportError:
    from sandweave_wire import decode, encode, MAX_BODY


class Agent:
    def __init__(self, root='/var/lib/sandweave/processes'):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.processes, self.lock = {}, threading.RLock()
        # A cold boot retains process logs but cannot retain their old liveness.
        # Memory restore resumes this object and never executes this initializer.
        for directory in self.root.iterdir():
            metadata = directory / 'process.json'
            if metadata.is_file() and not (directory / 'exit.json').exists():
                (directory / 'exit.json').write_text(json.dumps({
                    'returncode': -128, 'cold_boot': True, 'finished_ns': time.monotonic_ns()}))
        if os.getpid() == 1:
            threading.Thread(target=self.reap_orphans, daemon=True).start()

    def reap_orphans(self):
        while True:
            with self.lock:
                owned = {process.pid for process in self.processes.values()}
                for task in Path('/proc/self/task').glob('*/children'):
                    try:
                        children = list(map(int, task.read_text().split()))
                    except FileNotFoundError:
                        continue
                    for pid in children:
                        if pid not in owned:
                            try:
                                os.waitpid(pid, os.WNOHANG)
                            except ChildProcessError:
                                pass
            time.sleep(.1)

    def directory(self, identity):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', identity):
            raise ValueError('invalid process ID')
        return self.root / identity

    def spawn(self, identity, argv, cwd='/workspace', env=None, user='root', timeout=None):
        if not argv or any(not isinstance(a, str) or '\0' in a for a in argv):
            raise ValueError('argv must contain literal strings')
        if timeout is not None and (not isinstance(timeout, (float, int)) or timeout <= 0):
            raise ValueError('timeout must be positive')
        request = {'argv': argv, 'cwd': cwd, 'env': env or {}, 'user': user, 'timeout': timeout}
        directory = self.directory(identity)
        with self.lock:
            if directory.exists():
                if json.loads((directory / 'request.json').read_text()) != request:
                    raise ValueError('process ID was already used for another command')
                return self.status(identity)
            directory.mkdir()
            (directory / 'request.json').write_text(json.dumps(request))
            account = pwd.getpwuid(int(user)) if str(user).isdigit() else pwd.getpwnam(user)
            child_env = {**os.environ, 'HOME': account.pw_dir, 'USER': account.pw_name,
                         'LOGNAME': account.pw_name, **(env or {})}
            kwargs = {}
            if os.geteuid() == 0:
                kwargs = {'user': account.pw_uid, 'group': account.pw_gid,
                          'extra_groups': os.getgrouplist(account.pw_name, account.pw_gid)}
            stdout, stderr = (directory / 'stdout').open('wb'), (directory / 'stderr').open('wb')
            try:
                process = subprocess.Popen(argv, cwd=cwd, env=child_env, stdin=subprocess.PIPE,
                                           stdout=stdout, stderr=stderr, start_new_session=True,
                                           **kwargs)
            except BaseException:
                stdout.close(); stderr.close()
                now = time.monotonic_ns()
                (directory / 'process.json').write_text(json.dumps({'id': identity, 'pid': None, 'started_ns': now}))
                (directory / 'exit.json').write_text(json.dumps({'returncode': 127, 'spawn_failed': True,
                                                               'finished_ns': now}))
                raise
            self.processes[identity] = process
            os.set_blocking(process.stdin.fileno(), False)
            started = time.monotonic_ns()
            (directory / 'process.json').write_text(json.dumps({'id': identity, 'pid': process.pid,
                                                              'started_ns': started}))

        def finish():
            timed_out = False
            try:
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                result = {'returncode': process.returncode, 'timed_out': timed_out,
                          'finished_ns': time.monotonic_ns(), 'started_ns': started}
                temporary = directory / 'exit.tmp'
                temporary.write_text(json.dumps(result))
                temporary.replace(directory / 'exit.json')
            finally:
                stdout.close(); stderr.close()
                if process.stdin:
                    process.stdin.close()
                with self.lock:
                    self.processes.pop(identity, None)
        threading.Thread(target=finish, daemon=True).start()
        return self.status(identity)

    def status(self, identity):
        directory = self.directory(identity)
        result = json.loads((directory / 'process.json').read_text())
        if (directory / 'exit.json').exists():
            result.update(json.loads((directory / 'exit.json').read_text()))
        else:
            result['returncode'] = None
        result['stdout_size'] = (directory / 'stdout').stat().st_size
        result['stderr_size'] = (directory / 'stderr').stat().st_size
        return result

    def output(self, identity, stream, offset=0, size=1024**2):
        if stream not in ('stdout', 'stderr') or not 0 <= size <= 4*1024**2 or offset < 0:
            raise ValueError('invalid output request')
        with (self.directory(identity) / stream).open('rb') as source:
            source.seek(offset)
            return source.read(size)

    def stdin(self, identity, data=b'', close=False):
        with self.lock:
            process = self.processes.get(identity)
        if process is None or process.stdin.closed:
            if close and not data:
                return 0
            raise BrokenPipeError('process stdin is closed')
        if data:
            try:
                written = os.write(process.stdin.fileno(), data)
            except BlockingIOError:
                written = 0
        else:
            written = 0
        if close:
            process.stdin.close()
        return written

    def terminate(self, identity):
        with self.lock:
            process = self.processes.get(identity)
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return self.status(identity)

    def file(self, op, path, data=b'', offset=0, size=4*1024**2, truncate=False, mode=None):
        path = Path(path)
        if not path.is_absolute() or offset < 0 or not 0 <= size <= 4*1024**2:
            raise ValueError('file paths must be absolute and chunks bounded')
        if op == 'read':
            with path.open('rb') as source:
                source.seek(offset)
                return source.read(size)
        if op == 'write':
            path.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if truncate else 0)
            with os.fdopen(os.open(path, flags, 0o644), 'wb') as dest:
                dest.seek(offset)
                dest.write(data)
            if mode is not None:
                path.chmod(mode)
            return len(data)
        if op == 'stat':
            info = path.stat()
            return {'size': info.st_size, 'mode': info.st_mode, 'mtime_ns': info.st_mtime_ns,
                    'directory': path.is_dir()}
        if op == 'list':
            return [str(p) for p in sorted(path.iterdir())]
        raise ValueError('unknown file operation')

    def call(self, operation, parameters):
        if operation == 'ping':
            return {'pid': os.getpid(), 'monotonic_ns': time.monotonic_ns()}
        if operation not in ('spawn', 'status', 'output', 'stdin', 'terminate', 'file'):
            raise ValueError('unknown agent operation')
        return getattr(self, operation)(**parameters)


def main(port, token):
    Path('/workspace').mkdir(mode=0o777, exist_ok=True)
    Path('/workspace').chmod(0o777)
    if not Path('/usr/local/bin/python').exists():
        Path('/usr/local/bin/python').symlink_to('/usr/bin/python3')
    agent = Agent()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def setup(self):
            super().setup()
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.connection.settimeout(60)
            if not hmac.compare_digest(self.headers.get('X-Sandweave-Token', ''), token):
                self.send_error(403)
                return
            try:
                length = int(self.headers.get('Content-Length', '-1'))
                if not 0 <= length <= MAX_BODY:
                    raise ValueError('invalid request size')
                data = self.rfile.read(length)
                if len(data) != length:
                    raise ValueError('incomplete request')
                request = decode(data)
                result = {'result': agent.call(request['op'], request.get('params', {}))}
            except Exception as error:
                result = {'error': {'kind': type(error).__name__, 'message': str(error)}}
            payload = encode(result)
            self.send_response(200)
            self.send_header('Content-Type', 'application/vnd.sandweave.frame')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(('0.0.0.0', int(port)), Handler)
    server.daemon_threads = True
    server.serve_forever(poll_interval=.1)

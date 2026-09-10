"""Guest-only command/file service. Processes own guest files, not host exec pipes."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import errno
import fcntl
import json
import os
from pathlib import Path
import pwd
import grp
import pty as terminal
import re
import select
import signal
import socket
import socketserver
import subprocess
import struct
import sys
import threading
import time
import termios

try:
    from sandweave.sandbox.wire import decode, encode, MAX_BODY
except ImportError:
    from sandweave_wire import decode, encode, MAX_BODY


def user_account(user):
    """Resolve OCI USER values, including numeric IDs absent from /etc/passwd."""
    name, separator, group = str(user).partition(':')
    try:
        account = pwd.getpwuid(int(name)) if name.isdigit() else pwd.getpwnam(name)
        uid, gid, home, login = account.pw_uid, account.pw_gid, account.pw_dir, account.pw_name
    except KeyError:
        if not name.isdigit() and name != 'root':
            raise ValueError('guest user does not exist: ' + name) from None
        uid, gid, home, login = int(name) if name.isdigit() else 0, 0, '/', name
    if separator:
        if not group:
            raise ValueError('guest group must not be empty')
        gid = int(group) if group.isdigit() else grp.getgrnam(group).gr_gid
    if not 0 <= uid < 2**32 - 1 or not 0 <= gid < 2**32 - 1:
        raise ValueError('guest user and group IDs must be valid Linux IDs')
    groups = [] if separator else os.getgrouplist(login, gid)
    return uid, gid, home, login, groups


class Agent:
    def __init__(self, root='/var/lib/sandweave/processes'):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.processes, self.handles, self.lock = {}, {}, threading.RLock()
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

    def spawn(self, identity, argv, cwd='/workspace', env=None, user='root', timeout=None, max_output_bytes=64*1024**2, pty=False):
        if not argv or any(not isinstance(a, str) or '\0' in a for a in argv):
            raise ValueError('argv must contain literal strings')
        if timeout is not None and (not isinstance(timeout, (float, int)) or timeout <= 0):
            raise ValueError('timeout must be positive')
        if type(max_output_bytes) is not int or max_output_bytes <= 0:
            raise ValueError('max_output_bytes must be a positive integer')
        if pty is True:
            pty = {'rows': 24, 'cols': 80}
        if pty:
            if not isinstance(pty, dict) or set(pty) - {'rows', 'cols'}:
                raise ValueError('pty must be True or a rows/cols mapping')
            pty = {'rows': pty.get('rows', 24), 'cols': pty.get('cols', 80)}
            if any(type(v) is not int or not 1 <= v <= 1000 for v in pty.values()):
                raise ValueError('terminal rows/cols must be integers in 1..1000')
        request = {'argv': argv, 'cwd': cwd, 'env': env or {}, 'user': user, 'timeout': timeout,
                   'max_output_bytes': max_output_bytes, 'pty': pty}
        uid, gid, home, login, groups = user_account(user)
        directory = self.directory(identity)
        with self.lock:
            if directory.exists():
                if json.loads((directory / 'request.json').read_text()) != request:
                    raise ValueError('process ID was already used for another command')
                return self.status(identity)
            directory.mkdir()
            (directory / 'request.json').write_text(json.dumps(request))
            child_env = {**os.environ, 'HOME': home, 'USER': login,
                         'LOGNAME': login, **(env or {})}
            kwargs = {}
            if os.geteuid() == 0:
                kwargs = {'user': uid, 'group': gid, 'extra_groups': groups}
            stdout, stderr = (directory / 'stdout').open('wb'), (directory / 'stderr').open('wb')
            master = slave = None
            try:
                launch = argv
                if pty:
                    master, slave = terminal.openpty()
                    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', pty['rows'], pty['cols'], 0, 0))
                    child_env.setdefault('TERM', 'xterm-256color')
                    # Acquire the controlling terminal after exec into a fresh
                    # interpreter, avoiding preexec_fn in this threaded agent.
                    launch = [sys.executable, '-I', '-S', '-c', 'import fcntl,termios,os,sys; '
                              'fcntl.ioctl(0,termios.TIOCSCTTY,0); '
                              'os.execvpe(sys.argv[1],sys.argv[1:],os.environ)', *argv]
                process = subprocess.Popen(launch, cwd=cwd, env=child_env, stdin=slave if pty else subprocess.PIPE,
                                           stdout=slave if pty else subprocess.PIPE,
                                           stderr=slave if pty else subprocess.PIPE, start_new_session=True,
                                           **kwargs)
                if pty:
                    process.stdin = os.fdopen(os.dup(master), 'wb', buffering=0)
                    process.stdout = os.fdopen(master, 'rb', buffering=0)
                process.sandweave_pty = bool(pty)
            except BaseException:
                if master is not None:
                    os.close(master)
                stdout.close(); stderr.close()
                now = time.monotonic_ns()
                (directory / 'process.json').write_text(json.dumps({'id': identity, 'pid': None, 'started_ns': now}))
                (directory / 'exit.json').write_text(json.dumps({'returncode': 127, 'spawn_failed': True,
                                                               'finished_ns': now}))
                raise
            finally:
                if slave is not None:
                    os.close(slave)
            self.processes[identity] = process
            os.set_blocking(process.stdin.fileno(), False)
            started = time.monotonic_ns()
            (directory / 'process.json').write_text(json.dumps({'id': identity, 'pid': process.pid,
                                                              'started_ns': started, 'pty': pty}))

        output_state = {'bytes': 0, 'limited': False}
        output_lock = threading.Lock()

        def drain(source, destination):
            try:
                while True:
                    if not select.select([source], [], [], .05)[0]:
                        if process.poll() is not None:
                            break
                        continue
                    try:
                        data = os.read(source.fileno(), 64*1024)
                    except BlockingIOError:
                        continue
                    except OSError as error:
                        if pty and error.errno == errno.EIO:
                            break  # Linux PTY EOF after the final slave closes.
                        raise
                    if not data:
                        break
                    with output_lock:
                        remaining = max_output_bytes - output_state['bytes']
                        written = min(len(data), remaining)
                        destination.write(data[:written]); destination.flush()
                        output_state['bytes'] += written
                        if written < len(data):
                            output_state['limited'] = True
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
            finally:
                source.close()

        drains = [threading.Thread(target=drain, args=pair, daemon=True)
                  for pair in ([(process.stdout, stdout)] if pty else
                               [(process.stdout, stdout), (process.stderr, stderr)])]
        for thread in drains:
            thread.start()

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
                for thread in drains:
                    thread.join()
                result = {'returncode': process.returncode, 'timed_out': timed_out,
                          'output_limited': output_state['limited'], 'max_output_bytes': max_output_bytes,
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
            if process.sandweave_pty:
                # PTYs have one duplex channel. Canonical terminal EOF is EOT;
                # raw-mode applications may instead require terminate().
                os.write(process.stdin.fileno(), b'\x04')
            process.stdin.close()
        return written

    def resize(self, identity, rows, cols):
        if any(type(v) is not int or not 1 <= v <= 1000 for v in (rows, cols)):
            raise ValueError('terminal rows/cols must be integers in 1..1000')
        process = self.processes[identity]
        if not process.sandweave_pty:
            raise ValueError('process does not have a terminal')
        fcntl.ioctl(process.stdin.fileno(), termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
        return {'rows': rows, 'cols': cols}

    def terminate(self, identity):
        with self.lock:
            process = self.processes.get(identity)
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return self.status(identity)

    def file(self, op, path=None, data=b'', offset=0, size=4*1024**2, truncate=False, mode=None,
             handle=None, whence=0):
        if op == 'open':
            if not Path(path).is_absolute() or mode not in ('rb', 'wb', 'ab', 'xb', 'r+b', 'w+b', 'a+b', 'x+b'):
                raise ValueError('invalid file mode or path')
            self.directory(handle)  # Validate opaque handle syntax.
            with self.lock:
                if handle not in self.handles:
                    self.handles[handle] = (path, mode, open(path, mode, buffering=0))
                elif self.handles[handle][:2] != (path, mode):
                    raise ValueError('file handle already used for another open')
            return handle
        if handle is not None:
            with self.lock:
                if op == 'close':
                    saved = self.handles.pop(handle, None)
                    if saved:
                        saved[2].close()
                    return None
                stream = self.handles[handle][2]
                if op == 'read' and 0 <= size <= 4*1024**2:
                    return stream.read(size)
                if op == 'write' and len(data) <= 4*1024**2:
                    return stream.write(data)
                if op == 'seek':
                    return stream.seek(offset, whence)
                if op == 'tell':
                    return stream.tell()
                if op == 'truncate':
                    return stream.truncate(size)
            raise ValueError('invalid open-file operation')
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
            info = path.lstat()
            return {'size': info.st_size, 'mode': info.st_mode, 'mtime_ns': info.st_mtime_ns,
                    'directory': path.is_dir(), 'symlink': path.is_symlink()}
        if op == 'list':
            return [str(p) for p in sorted(path.iterdir())]
        if op == 'mkdir':
            path.mkdir(parents=True, exist_ok=True)
            return None
        raise ValueError('unknown file operation')

    def call(self, operation, parameters):
        if operation == 'ping':
            return {'pid': os.getpid(), 'monotonic_ns': time.monotonic_ns()}
        if operation not in ('spawn', 'status', 'output', 'stdin', 'terminate', 'resize', 'file'):
            raise ValueError('unknown agent operation')
        return getattr(self, operation)(**parameters)


def main(port, token, image=False):
    if not image:
        Path('/workspace').mkdir(mode=0o777, exist_ok=True)
        Path('/workspace').chmod(0o777)
        if not Path('/usr/local/bin/python').exists():
            Path('/usr/local/bin/python').symlink_to('/usr/bin/python3')
    agent = Agent('/.sandweave-runtime/processes' if image else '/var/lib/sandweave/processes')

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        wbufsize = 64 * 1024

        def setup(self):
            super().setup()
            if self.connection.family == socket.AF_INET:
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
            # Bound a request body, not the idle lifetime of a persistent client.
            self.connection.settimeout(None)

    if str(port).startswith('/'):
        class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
        Path(port).unlink(missing_ok=True)
        server = UnixServer(str(port), Handler)
        Path(port).chmod(0o600)
    else:
        server = ThreadingHTTPServer(('0.0.0.0', int(port)), Handler)
    server.daemon_threads = True
    server.serve_forever(poll_interval=.1)

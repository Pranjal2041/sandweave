"""Guest-only command/file service. Processes own guest files, not host exec pipes."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import errno
import fcntl
import json
import math
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
import shutil
import stat
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


SERVICE_SUPERVISOR = '''import os,signal,subprocess,sys,time
policy, stop_signal, *command = sys.argv[1:]
mode, _, maximum = policy.partition(':')
maximum = int(maximum) if maximum else 0
stopping = False
child = None
def stop(number, frame):
    global stopping
    stopping = True
    if child is not None:
        try: child.send_signal(number)
        except ProcessLookupError: pass
for number in {signal.SIGTERM,signal.SIGINT,signal.SIGHUP,signal.SIGQUIT,int(stop_signal)}:
    if number not in (signal.SIGKILL,signal.SIGSTOP): signal.signal(number,stop)
attempt, delay = 0, .1
while not stopping:
    started = time.monotonic()
    child = subprocess.Popen(command)
    result = child.wait()
    attempt += 1
    if stopping or mode=='no' or (mode=='on-failure' and (result==0 or maximum and attempt>maximum)):
        sys.exit(result if result>=0 else 128-result)
    if time.monotonic()-started>=10: delay=.1
    time.sleep(delay)
    delay=min(delay*2,1)
sys.exit(0)
'''


class Agent:
    def __init__(self, root='/var/lib/sandweave/processes'):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.processes, self.handles, self.lock = {}, {}, threading.RLock()
        self.service_process = None
        self.service_limits, self.service_groups = {}, []
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
                if self.service_process is not None:
                    self._service_exited()
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

    def service_setup(self, mounts=(), hostname=None, tmpfs=(), extra_hosts=None, read_only=False,
                      watch_process=None, ulimits=None, group_add=(), restart='no', stop_signal='SIGTERM'):
        if watch_process is not None:
            with self.lock:
                self.directory(watch_process)
                self.status(watch_process)
                if self.service_process is not None and self.service_process != watch_process:
                    raise ValueError('service entrypoint is already registered')
                self.service_process = watch_process
                self._service_exited()
            return {'ready': True}
        import resource
        limits = {}
        for name, value in (ulimits or {}).items():
            number = getattr(resource, 'RLIMIT_' + name.upper(), None)
            if number is None:
                raise ValueError('unsupported process resource limit: ' + name)
            values = [value['soft'], value['hard']] if isinstance(value, dict) else [value, value]
            if any(type(item) is not int or item < -1 for item in values):
                raise ValueError('invalid process resource limit: ' + name)
            limits[number] = values
        groups = [int(group) if str(group).isdigit() else grp.getgrnam(group).gr_gid for group in group_add]
        if any(not 0 <= group < 2**32 - 1 for group in groups):
            raise ValueError('invalid additional group ID')
        self.service_limits, self.service_groups = limits, groups
        supervisor = None
        if restart and restart != 'no':
            if not re.fullmatch(r'(always|unless-stopped|on-failure(?::[0-9]+)?)', restart):
                raise ValueError('invalid service restart policy')
            selected = str(stop_signal)
            number = int(selected) if selected.isdigit() else getattr(signal,
                selected if selected.startswith('SIG') else 'SIG' + selected)
            script = self.root.parent / 'tools/service-supervisor.py'
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text(SERVICE_SUPERVISOR)
            script.chmod(0o644)
            supervisor = [sys.executable, str(script), restart, str(number)]
        helper = str(self.root.parent / 'tools/guest-tools')
        def run(*arguments):
            subprocess.run([helper, *arguments], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def copy(source, destination, links):
            info = source.lstat()
            if stat.S_ISLNK(info.st_mode):
                destination.symlink_to(os.readlink(source))
            elif stat.S_ISDIR(info.st_mode):
                destination.mkdir(exist_ok=True)
                for child in source.iterdir():
                    copy(child, destination / child.name, links)
            elif stat.S_ISREG(info.st_mode):
                key = (info.st_dev, info.st_ino)
                if info.st_nlink > 1 and key in links:
                    os.link(links[key], destination)
                else:
                    shutil.copyfile(source, destination)
                    links[key] = destination
            elif stat.S_ISFIFO(info.st_mode):
                os.mkfifo(destination)
            else:
                os.mknod(destination, info.st_mode, info.st_rdev)
            os.chown(destination, info.st_uid, info.st_gid, follow_symlinks=False)
            shutil.copystat(source, destination, follow_symlinks=False)
        if hostname is not None:
            run('hostname', hostname)
        if extra_hosts:
            if isinstance(extra_hosts, list):
                entries = []
                for entry in extra_hosts:
                    host, separator, address = entry.partition('=')
                    if not separator:
                        host, separator, address = entry.partition(':')
                    if not separator:
                        raise ValueError('invalid extra_hosts entry: ' + entry)
                    entries.append((host, address))
            else:
                entries = extra_hosts.items()
            with open('/etc/hosts', 'a') as output:
                for host, addresses in entries:
                    for address in addresses if isinstance(addresses, list) else [addresses]:
                        output.write(str(address) + ' ' + host + '\n')
        for mount in sorted(mounts, key=lambda item: len(Path(item['target']).parts)):
            source, target = Path(mount['staging']), Path(mount['target'])
            if mount.get('subpath'):
                relative = Path(mount['subpath'])
                if relative.is_absolute() or '..' in relative.parts:
                    raise ValueError('volume subpath must stay within the volume')
                source = source / relative
                if not source.exists():
                    raise FileNotFoundError(source)
            if mount.get('copy') and target.is_dir() and not any(source.iterdir()):
                copy(target, source, {})
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                target.mkdir(exist_ok=True)
            else:
                target.touch(exist_ok=True)
            if mount.get('uid') is not None or mount.get('gid') is not None:
                os.chown(source, int(mount.get('uid', -1)), int(mount.get('gid', -1)))
            if mount.get('mode') is not None:
                source.chmod(mount['mode'])
            run('bind', str(source), str(target))
            if mount.get('read_only'):
                run('readonly', str(target))
        for source in dict.fromkeys(mount['staging'] for mount in mounts):
            run('unmount', source)
        for mount in tmpfs:
            Path(mount['target']).mkdir(parents=True, exist_ok=True)
            run('tmpfs', mount['target'], mount.get('options', ''))
        if read_only:
            # Process logs/control state remain writable after the image root
            # becomes read-only, just as Docker's writable log mounts do.
            control = str(self.root.parent)
            run('bind', control, control)
            run('root-readonly')
        return {'ready': True, 'supervisor': supervisor}

    def directory(self, identity):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', identity):
            raise ValueError('invalid process ID')
        return self.root / identity

    def _service_exited(self):
        if self.service_process is None:
            return False
        main = self.processes.get(self.service_process)
        if main is not None and main.poll() is None:
            return False
        for process in self.processes.values():
            if not getattr(process, 'sandweave_maintenance', False):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        # A service can double-fork or create a new session. Killing only the
        # original process groups misses those descendants. Each service owns
        # its entire guest PID namespace; preserve the command service and any
        # active artifact-transfer trees, then stop the remaining guest tasks.
        # Only PID 1 owns that namespace. A service instantiated for host-side
        # protocol tests, or embedded in another init, must never sweep /proc.
        if os.getpid() != 1:
            return True
        parents = {}
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
                parents[int(entry.name)] = int(fields[1])
            except (FileNotFoundError, ProcessLookupError):
                continue
        protected = {1, os.getpid()}
        parent = parents.get(os.getpid(), 0)
        while parent and parent not in protected:
            protected.add(parent)
            parent = parents.get(parent, 0)
        transfers = {process.pid for process in self.processes.values()
                     if getattr(process, 'sandweave_maintenance', False) and process.poll() is None}
        while True:
            descendants = {pid for pid, parent in parents.items() if parent in transfers}
            if descendants <= transfers:
                break
            transfers.update(descendants)
        for pid in parents.keys() - protected - transfers:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return True

    def spawn(self, identity, argv, cwd='/workspace', env=None, user='root', timeout=None, max_output_bytes=64*1024**2, pty=False, maintenance=False):
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
        groups = sorted(set(groups).union(self.service_groups))
        directory = self.directory(identity)
        with self.lock:
            if directory.exists():
                if json.loads((directory / 'request.json').read_text()) != request:
                    raise ValueError('process ID was already used for another command')
                return self.status(identity)
            if not maintenance and self._service_exited():
                raise RuntimeError('service entrypoint has exited; the service is stopped')
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
                if self.service_limits and not maintenance:
                    # Apply limits before dropping IDs, in a fresh process.
                    # Avoid preexec_fn in the threaded guest agent.
                    account = [uid, gid, groups] if os.geteuid() == 0 else None
                    launch = [sys.executable, '-I', '-S', '-c',
                              'import json,os,resource,sys; limits,account=json.loads(sys.argv[1]); '
                              '[resource.setrlimit(int(key),value) for key,value in limits.items()]; '
                              '(os.setgroups(account[2]),os.setgid(account[1]),os.setuid(account[0])) if account else None; '
                              'os.execvpe(sys.argv[2],sys.argv[2:],os.environ)',
                              json.dumps([self.service_limits, account]), *launch]
                    kwargs = {}
                process = subprocess.Popen(launch, cwd=cwd, env=child_env, stdin=slave if pty else subprocess.PIPE,
                                           stdout=slave if pty else subprocess.PIPE,
                                           stderr=slave if pty else subprocess.PIPE, start_new_session=True,
                                           **kwargs)
                if pty:
                    process.stdin = os.fdopen(os.dup(master), 'wb', buffering=0)
                    process.stdout = os.fdopen(master, 'rb', buffering=0)
                process.sandweave_pty = bool(pty)
                process.sandweave_maintenance = maintenance
                process.sandweave_done = threading.Event()
                process.sandweave_output = threading.Condition()
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
                        with process.sandweave_output:
                            process.sandweave_output.notify_all()
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
                with self.lock:
                    if self.service_process == identity:
                        self._service_exited()
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
                process.sandweave_done.set()
                with process.sandweave_output:
                    process.sandweave_output.notify_all()
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

    def wait(self, identity, timeout=10, stream=None, offset=0):
        """Sleep until exit or new stream bytes, with no status polling."""
        if (not isinstance(timeout, (int, float)) or not math.isfinite(timeout)
                or not 0 <= timeout <= 10 or stream not in (None, 'stdout', 'stderr')
                or type(offset) is not int or offset < 0):
            raise ValueError('invalid process wait')
        with self.lock:
            process = self.processes.get(identity)
        if process is not None:
            if stream is None:
                process.sandweave_done.wait(timeout)
            else:
                path = self.directory(identity) / stream
                with process.sandweave_output:
                    process.sandweave_output.wait_for(
                        lambda: process.sandweave_done.is_set() or path.stat().st_size > offset,
                        timeout=timeout)
        return self.status(identity)

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

    def terminate(self, identity, signal_number=signal.SIGKILL):
        signal_number = int(signal_number)
        if not 1 <= signal_number < signal.NSIG:
            raise ValueError('invalid signal number')
        with self.lock:
            process = self.processes.get(identity)
        if process is not None:
            try:
                os.killpg(process.pid, signal_number)
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
        if operation not in ('spawn', 'status', 'wait', 'output', 'stdin', 'terminate', 'resize', 'file', 'service_setup'):
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

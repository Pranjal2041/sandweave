"""Launch worker subprocesses without inheriting the worker's storage handles.

Install once, before starting worker threads. A small fork server has no open
storage files. Each launch gets a fresh helper, which receives the explicitly requested
descriptors *after* it forks. Even pre-exec descriptor closes cannot touch an
unrelated image upload. The SDK client's subprocess module is never changed.
"""
import array
import fcntl
import os
import pickle
import resource
import selectors
import signal
import socket
import struct
import sys


def _send(channel, value):
    data = pickle.dumps(value, protocol=5)
    channel.sendall(struct.pack('!I', len(data)) + data)


def _read(channel, size):
    data = bytearray()
    while len(data) < size:
        part = channel.recv(size - len(data))
        if not part:
            raise ChildProcessError('worker launch helper disconnected')
        data.extend(part)
    return bytes(data)


def _receive(channel):
    # Only inherited private socketpairs carry these messages; never RPC input.
    return pickle.loads(_read(channel, struct.unpack('!I', _read(channel, 4))[0]))


def _send_fds(channel, fds):
    for offset in range(0, len(fds), 200):
        channel.sendmsg([b'F'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                 array.array('i', fds[offset:offset + 200]))])


def _receive_fds(channel, count):
    result = []
    try:
        while len(result) < count:
            data, ancillary, flags, _ = channel.recvmsg(1, socket.CMSG_SPACE(200 * array.array('i').itemsize),
                                                       socket.MSG_CMSG_CLOEXEC)
            if not data:
                raise EOFError('worker launch channel closed')
            for level, kind, payload in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    values = array.array('i')
                    values.frombytes(payload)
                    result.extend(values)
            if data != b'F' or flags & socket.MSG_CTRUNC or len(result) > count:
                raise ChildProcessError('invalid launch descriptor message')
        return result
    except BaseException:
        for fd in result:
            os.close(fd)
        raise


def _launch(channel, errors):
    opened = []
    try:
        request = _receive(channel)
        received = _receive_fds(channel, len(request['fds']))
        opened.extend(received)
        # Preserve pass_fds numbers (some commands refer to them in argv/env).
        # Move our control channel and received handles out of their way first.
        minimum = max([2, *request['options']['pass_fds']]) + 1
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        needed = minimum + len(received) + 1
        if soft != resource.RLIM_INFINITY and soft < needed:
            resource.setrlimit(resource.RLIMIT_NOFILE, (needed, hard))
        moved = socket.socket(fileno=fcntl.fcntl(errors.fileno(), fcntl.F_DUPFD_CLOEXEC, minimum))
        errors.close()
        errors = moved
        mapping = {}
        for original, fd in zip(request['fds'], received):
            mapping[original] = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, minimum)
            opened.append(mapping[original])
        for fd in received:
            os.close(fd)
            opened.remove(fd)
        channel.close()
        options = request['options']
        for original in options['pass_fds']:
            if original > 2:
                os.dup2(mapping[original], original, inheritable=True)
        for target, name in enumerate(('stdin', 'stdout', 'stderr')):
            os.dup2(mapping[options[name]], target, inheritable=True)
        for fd in opened:
            os.close(fd)
        opened.clear()
        for limit, value in request['limits']:
            if resource.getrlimit(limit) != value:
                resource.setrlimit(limit, value)
        os.sched_setaffinity(0, request['affinity'])
        signal.pthread_sigmask(signal.SIG_SETMASK, request['signal_mask'])
        if options['restore_signals']:
            for name in ('SIGPIPE', 'SIGXFZ', 'SIGXFSZ'):
                if hasattr(signal, name):
                    signal.signal(getattr(signal, name), signal.SIG_DFL)
        if options['start_new_session']:
            os.setsid()
        if options['process_group'] >= 0:
            os.setpgid(0, options['process_group'])
        elif not options['start_new_session'] and os.getpgrp() != request['parent_group']:
            os.setpgid(0, request['parent_group'])
        if options['group'] is not None:
            os.setregid(options['group'], options['group'])
        if options['extra_groups'] is not None:
            os.setgroups(options['extra_groups'])
        if options['user'] is not None:
            os.setreuid(options['user'], options['user'])
        os.umask(options['umask'])
        os.chdir(options['cwd'])
        argv = options['args']
        argv = [argv] if isinstance(argv, (str, bytes, os.PathLike)) else list(argv)
        if options['shell']:
            argv = ['/bin/sh', '-c', *argv]
            if options['executable'] is not None:
                argv[0] = options['executable']
        executable = argv[0] if options['executable'] is None else options['executable']
        # The helper becomes the command itself. Its error socket closes at
        # exec, allowing the server to acknowledge startup without polling.
        os.execvpe(executable, argv, options['env'])
    except BaseException as error:
        try:
            _send(errors, ('error', error))
        except (OSError, ChildProcessError):
            pass
    finally:
        for fd in opened:
            os.close(fd)
        channel.close()
        errors.close()


def _serve(channel):
    selector = selectors.DefaultSelector()
    wake, wake_write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
    signal.signal(signal.SIGCHLD, lambda *_: None)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGCHLD})
    signal.set_wakeup_fd(wake_write)
    selector.register(channel, selectors.EVENT_READ, ('request', None))
    selector.register(wake, selectors.EVENT_READ, ('reap', None))
    children = {}
    high_fd = max(channel.fileno(), selector.fileno(), wake, wake_write)

    def drop_socket(sock):
        try:
            selector.unregister(sock)
        except KeyError:
            pass
        sock.close()

    def reply(state, value):
        if state['channel'] is not None:
            try:
                _send(state['channel'], value)
            except (BrokenPipeError, ConnectionResetError):
                drop_socket(state['channel'])
                state['channel'] = None

    def complete(pid, state):
        if state['returncode'] is not None and state['started'] is not None:
            if state['started']:
                reply(state, ('exit', state['returncode']))
            if state['channel'] is not None:
                drop_socket(state['channel'])
            children.pop(pid)

    _send(channel, 'ready')
    while True:
        for key, _ in selector.select():
            kind, pid = key.data
            if kind == 'request':
                try:
                    request, = _receive_fds(channel, 1)
                except EOFError:
                    return
                except ChildProcessError:
                    # Descriptor pressure may truncate SCM_RIGHTS. That
                    # request loses its peer, but the server remains usable.
                    continue
                child = socket.socket(fileno=request)
                parent_error = child_error = None
                try:
                    parent_error, child_error = socket.socketpair()
                    high_fd = max(high_fd, request, parent_error.fileno(), child_error.fileno())
                    pid = os.fork()
                except OSError as error:
                    try:
                        _send(child, ('error', error))
                    except OSError:
                        pass
                    child.close()
                    if parent_error is not None:
                        parent_error.close()
                        child_error.close()
                    continue
                if pid == 0:
                    signal.set_wakeup_fd(-1)
                    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
                    # Close the server's sockets before receiving any files.
                    # Use native close ranges, not a Python loop over every
                    # other running command. This branch always execs/_exits;
                    # inherited Python socket wrappers never run destructors.
                    first = 3
                    for keep in sorted((child.fileno(), child_error.fileno())):
                        os.closerange(first, keep)
                        first = keep + 1
                    os.closerange(first, high_fd + 1)
                    try:
                        _launch(child, child_error)
                    finally:
                        os._exit(255)
                child_error.close()
                children[pid] = {'channel': child, 'error': parent_error, 'started': None, 'returncode': None}
                selector.register(parent_error, selectors.EVENT_READ, ('exec', pid))
            elif kind == 'reap':
                os.read(wake, 65536)
                while True:
                    try:
                        pid, status = os.waitpid(-1, os.WNOHANG)
                    except ChildProcessError:
                        break
                    if not pid:
                        break
                    state = children[pid]
                    state['returncode'] = os.waitstatus_to_exitcode(status)
                    complete(pid, state)
            elif kind == 'exec':
                state = children[pid]
                error = state['error']
                if error.recv(1, socket.MSG_PEEK):
                    state['started'] = False
                    reply(state, _receive(error))
                else:
                    state['started'] = True
                    reply(state, ('started', pid))
                    if state['channel'] is not None:
                        selector.register(state['channel'], selectors.EVENT_READ, ('signal', pid))
                drop_socket(error)
                state['error'] = None
                complete(pid, state)
            elif kind == 'signal' and pid in children:
                state = children[pid]
                try:
                    message = state['channel'].recv(4)
                    if message:
                        message += _read(state['channel'], 4 - len(message))
                except (ChildProcessError, ConnectionResetError):
                    message = b''
                if not message:
                    drop_socket(state['channel'])
                    state['channel'] = None
                elif state['returncode'] is None:
                    signum = struct.unpack('!I', message)[0]
                    try:
                        os.kill(pid, signum)
                    except ProcessLookupError:
                        pass


if __name__ == '__main__':
    # Keep the fork server small: it does not need the client's subprocess,
    # inspection or threading machinery in every child's inherited heap.
    _serve(socket.socket(fileno=int(sys.argv[1])))
    sys.exit(0)


import atexit
import inspect
import math
import select
import subprocess
import threading
import time

_Popen = subprocess.Popen
_execute_signature = inspect.signature(_Popen._execute_child)
_server = None
_limits = sorted({getattr(resource, name) for name in dir(resource) if name.startswith('RLIMIT_')})


class Launcher:
    def __init__(self):
        self.channel, remote = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            self.process = _Popen([sys.executable, '-I', '-S', __file__, str(remote.fileno())],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, pass_fds=(remote.fileno(),))
        finally:
            remote.close()
        # The readiness message is one packet on this bootstrap socket.
        packet = self.channel.recv(1024)
        if not packet or pickle.loads(packet[4:]) != 'ready':
            raise ChildProcessError('worker launcher did not start')

    def request(self, message):
        channel, remote = socket.socketpair()
        try:
            _send_fds(self.channel, [remote.fileno()])
            _send(channel, message)
            _send_fds(channel, message['fds'])
            return channel
        except BaseException:
            channel.close()
            raise
        finally:
            remote.close()

    def close(self):
        self.channel.close()
        self.process.wait(timeout=10)


class Popen(_Popen):
    """Keep stdlib pipes, communicate, errors and timeouts; delegate spawning."""
    def _execute_child(self, *args, **kwargs):
        values = _execute_signature.bind(self, *args, **kwargs).arguments
        if values['preexec_fn'] is not None:
            raise ValueError('preexec_fn is unsupported in a threaded Sandweave worker')
        if not values['close_fds']:
            raise ValueError('worker subprocesses must use close_fds=True; use pass_fds for explicit handles')
        handles = [values[name] for name in ('p2cread', 'c2pwrite', 'errwrite')]
        handles = [number if number >= 0 else index for index, number in enumerate(handles)]
        options = {key: values[key] for key in ('args', 'executable', 'close_fds', 'pass_fds', 'shell',
                                               'restore_signals', 'start_new_session', 'process_group')}
        options.update(cwd=values['cwd'] if values['cwd'] is not None else os.getcwd(),
                       env=values['env'] if values['env'] is not None else dict(os.environ),
                       user=values['uid'], group=values['gid'], extra_groups=values['gids'],
                       umask=values['umask'], stdin=handles[0], stdout=handles[1], stderr=handles[2])
        argv = options['args']
        argv = [argv] if isinstance(argv, (str, bytes, os.PathLike)) else list(argv)
        options['args'] = [os.fsencode(arg) for arg in argv]
        options['cwd'] = os.fsencode(options['cwd'])
        options['env'] = {os.fsencode(key): os.fsencode(value) for key, value in options['env'].items()}
        if options['executable'] is not None:
            options['executable'] = os.fsencode(options['executable'])
        if options['umask'] < 0:
            with open('/proc/self/status') as status:
                options['umask'] = next(int(line.split()[1], 8) for line in status if line.startswith('Umask:'))
        message = {'options': options, 'fds': sorted(set([*handles, *values['pass_fds']])),
                   'affinity': sorted(os.sched_getaffinity(0)), 'parent_group': os.getpgrp(),
                   'limits': [(limit, resource.getrlimit(limit)) for limit in _limits],
                   'signal_mask': [int(sig) for sig in signal.pthread_sigmask(signal.SIG_BLOCK, set())]}
        self._launch_channel = _server.request(message)
        self._signal_lock = threading.Lock()
        try:
            kind, value = _receive(self._launch_channel)
            if kind != 'started':
                raise value
            self.pid = value
            self._child_created = True
        except BaseException:
            self._launch_channel.close()
            raise
        finally:
            self._close_pipe_fds(*(values[name] for name in
                                  ('p2cread', 'p2cwrite', 'c2pread', 'c2pwrite', 'errread', 'errwrite')))

    def _result(self, timeout):
        if self.returncode is not None:
            return self.returncode
        poller = select.poll()  # select() rejects descriptor numbers above 1023.
        poller.register(self._launch_channel, select.POLLIN)
        if poller.poll(None if timeout is None else math.ceil(timeout * 1000)):
            kind, value = _receive(self._launch_channel)
            if kind != 'exit':
                raise value
            self.returncode = value
            with self._signal_lock:
                self._launch_channel.close()
        return self.returncode

    def _internal_poll(self, _deadstate=None):
        if self.returncode is None and self._waitpid_lock.acquire(False):
            try:
                self._result(0)
            except (OSError, ChildProcessError):
                if _deadstate is not None:
                    self.returncode = _deadstate
                else:
                    raise
            finally:
                self._waitpid_lock.release()
        return self.returncode

    def _wait(self, timeout):
        deadline = None if timeout is None else time.monotonic() + timeout
        if not self._waitpid_lock.acquire(timeout=-1 if timeout is None else max(0, timeout)):
            raise subprocess.TimeoutExpired(self.args, timeout)
        try:
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            if self._result(remaining) is None:
                raise subprocess.TimeoutExpired(self.args, timeout)
            return self.returncode
        finally:
            self._waitpid_lock.release()

    def send_signal(self, sig):
        with self._signal_lock:
            if self.returncode is None:
                try:
                    self._launch_channel.sendall(struct.pack('!I', sig))
                except BrokenPipeError:
                    pass


def install():
    """Called by the worker entry point before storage work or request threads."""
    global _server
    if _server is None:
        _server = Launcher()
        atexit.register(_server.close)
        subprocess.Popen = Popen

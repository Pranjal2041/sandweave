"""Local Xvnc fast I/O: persistent XTEST helper and host-visible MIT-SHM frames."""
from contextlib import contextmanager
import ctypes
import ctypes.util
import fcntl
import hashlib
import io
import json
import mmap
import os
from pathlib import Path
import select
import signal
import socket
import struct
import subprocess
import sys
import tarfile
import time
import uuid

import environment_control as control

FRAME_BYTES = 64 + 64 * 1024 * 1024
MAX_MESSAGE = 1024 * 1024
HEADER = struct.Struct('<QIIIIQQQ')
EVENT = struct.Struct('<IIii')
KEYS = {'ctrl': 'Control_L', 'control': 'Control_L', 'shift': 'Shift_L',
        'alt': 'Alt_L', 'super': 'Super_L', 'meta': 'Super_L', 'win': 'Super_L',
        'enter': 'Return', 'return': 'Return', 'esc': 'Escape', 'escape': 'Escape',
        'tab': 'Tab', 'space': 'space', 'backspace': 'BackSpace', 'delete': 'Delete',
        'insert': 'Insert', 'home': 'Home', 'end': 'End', 'pageup': 'Prior',
        'pagedown': 'Next', 'left': 'Left', 'right': 'Right', 'up': 'Up', 'down': 'Down',
        'capslock': 'Caps_Lock', 'print': 'Print', 'pause': 'Pause'}
SHIFTED = dict(zip('~!@#$%^&*()_+{}|:"<>?', '`1234567890-=[]\\;\',./'))
_x11 = None


def keysym(key):
    global _x11
    if not isinstance(key, str) or not key:
        raise ValueError('key names must be nonempty strings')
    if len(key) == 1:
        value = ord(key)
        return value if value < 256 else 0x01000000 | value
    if _x11 is None:
        _x11 = ctypes.CDLL(ctypes.util.find_library('X11'))
        _x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
        _x11.XStringToKeysym.restype = ctypes.c_ulong
    name = KEYS.get(key.lower(), key)
    if key.lower().startswith('f') and key[1:].isdigit():
        name = key.upper()
    value = _x11.XStringToKeysym(name.encode())
    if not value:
        raise ValueError('unknown key: ' + key)
    return value


def events_for_action(action):
    """Gym-style mouse/keyboard dictionaries, or a list making one fenced batch."""
    events = []

    def key_event(key, down):
        events.append((4 if down else 5, keysym(key), 0, 0))

    def motion(position):
        if not isinstance(position, (list, tuple)) or len(position) != 2:
            raise ValueError('position requires [x, y]')
        if any(type(v) is not int or v < 0 or v > 32767 for v in position):
            raise ValueError('coordinates must be integers in 0..32767')
        events.append((1, 0, *position))

    def button(number, down):
        events.append((2 if down else 3, number, 0, 0))

    actions = action if isinstance(action, list) else [action]
    for item in actions:
        if not isinstance(item, dict) or set(item) - {'mouse', 'keyboard'}:
            raise ValueError('actions support mouse and keyboard only')
        mouse = item.get('mouse', {})
        keyboard = item.get('keyboard', {})
        if not isinstance(mouse, dict) or not isinstance(keyboard, dict):
            raise ValueError('mouse and keyboard must be dictionaries')
        for kind, value in mouse.items():
            if kind == 'move':
                motion(value)
            elif kind in ('left_click', 'middle_click', 'right_click', 'double_click', 'triple_click'):
                motion(value)
                number = {'middle_click': 2, 'right_click': 3}.get(kind, 1)
                for _ in range({'double_click': 2, 'triple_click': 3}.get(kind, 1)):
                    button(number, True); button(number, False)
            elif kind in ('left_click_drag', 'right_click_drag'):
                if not isinstance(value, (list, tuple)) or len(value) < 2:
                    raise ValueError('drag requires at least two positions')
                motion(value[0]); number = 1 if kind.startswith('left') else 3
                button(number, True)
                for start, end in zip(value, value[1:]):
                    motion(end)  # Validate before interpolation.
                    events.pop()
                    for step in range(1, 9):
                        motion([round(start[d] + (end[d] - start[d]) * step / 8) for d in (0, 1)])
                button(number, False)
            elif kind == 'scroll':
                if type(value) is not int or abs(value) > 1000:
                    raise ValueError('scroll must be an integer in -1000..1000; positive scrolls down')
                for _ in range(abs(value)):
                    button(5 if value > 0 else 4, True); button(5 if value > 0 else 4, False)
            elif kind == 'buttons':
                values = [value] if isinstance(value, str) else value
                for entry in values:
                    try:
                        name, direction = entry.rsplit('_', 1)
                        number = {'left': 1, 'middle': 2, 'right': 3}[name]
                        if direction not in ('down', 'up'):
                            raise ValueError()
                    except (KeyError, ValueError, AttributeError):
                        raise ValueError('buttons require left/middle/right_down/up') from None
                    button(number, direction == 'down')
            else:
                raise ValueError('unknown mouse action: ' + kind)
        for kind, value in keyboard.items():
            if kind == 'text':
                if not isinstance(value, str) or len(value) > 2048:
                    raise ValueError('text must contain at most 2048 characters')
                for char in value:
                    char = {'\n': 'Return', '\r': 'Return', '\t': 'Tab', '\b': 'BackSpace'}.get(char, char)
                    shift = char in SHIFTED or (len(char) == 1 and 'A' <= char <= 'Z')
                    key = SHIFTED.get(char, char.lower() if shift else char)
                    if shift:
                        key_event('shift', True)
                    key_event(key, True); key_event(key, False)
                    if shift:
                        key_event('shift', False)
            elif kind in ('keys', 'keys_down', 'keys_up'):
                values = [value] if isinstance(value, str) else value
                if not isinstance(values, (list, tuple)):
                    raise ValueError('keys require a string or list of strings')
                if kind != 'keys_up':
                    for key in values:
                        key_event(key, True)
                if kind != 'keys_down':
                    for key in reversed(values):
                        key_event(key, False)
            else:
                raise ValueError('unknown keyboard action: ' + kind)
        if len(events) > 8192:
            raise ValueError('action exceeds 8192 input events')
    return events


def directory(manager, name):
    path = manager.local / 'gvisor/fast-io' / control.valid_name(name)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _read_json(stream):
    line = stream.readline(MAX_MESSAGE + 1)
    if not line or len(line) > MAX_MESSAGE or not line.endswith(b'\n'):
        raise ConnectionError('fast I/O disconnected or sent an invalid reply')
    return json.loads(line)


def _socket_path(manager, name):
    return directory(manager, name) / 'control.sock'


def _rpc(manager, name, request, *, start=False):
    if start:
        ensure_service(manager, name)
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(15)
        try:
            connection.connect(str(_socket_path(manager, name)))
        except (FileNotFoundError, ConnectionRefusedError):
            if start or request['op'] in ('ping', 'detach', 'stop'):
                raise
            # Nothing has been sent: reconnecting here cannot replay an action.
            ensure_service(manager, name)
            connection.connect(str(_socket_path(manager, name)))
        connection.sendall(json.dumps(request, separators=(',', ':')).encode() + b'\n')
        with connection.makefile('rb') as stream:
            reply = _read_json(stream)
    if 'error' in reply:
        raise RuntimeError(reply['error'])
    return reply


def ensure_service(manager, name):
    path = directory(manager, name)
    with (path / 'startup.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            _rpc(manager, name, {'op': 'ping'})
            return
        except (FileNotFoundError, ConnectionRefusedError):
            pass
        if manager.status(name)['status'] not in ('running', 'paused'):
            raise ValueError('fast I/O requires a started environment')
        with (manager._logs(name) / 'fast-io.log').open('ab', buffering=0) as log:
            child = subprocess.Popen([sys.executable, str(manager.lab / 'scripts/fastio.py'),
                                      '_serve', name], cwd=manager.lab, stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=log, start_new_session=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError('fast I/O service exited; inspect fast-io.log')
            try:
                _rpc(manager, name, {'op': 'ping'})
                return
            except (FileNotFoundError, ConnectionRefusedError):
                time.sleep(.02)
        child.terminate()
        raise TimeoutError('fast I/O service did not start')


def detach(manager, name, *, stop=False, discard=False):
    """Caller holds the exclusive lifecycle lock; no new I/O can attach."""
    try:
        return _rpc(manager, name, {'op': 'stop' if stop else 'detach', 'discard': discard})
    except (FileNotFoundError, ConnectionRefusedError):
        return None


@contextmanager
def io_lock(manager, name):
    path = manager.local / 'gvisor/operations'
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (path / (control.valid_name(name) + '.lock')).open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('environment lifecycle operation in progress') from None
        yield


class Bridge:
    def __init__(self, manager, name):
        self.manager, self.name = manager, name
        self.ready = False
        self.broken = False
        self.closed = False
        assets = manager.lab / 'tools/fast-io'
        if not (assets / 'bridge').exists():
            raise RuntimeError('run scripts/build-fast-io.sh first')
        digest = hashlib.sha256()
        for file in ('bridge', 'libxcb-xtest.so.0'):
            digest.update((assets / file).read_bytes())
        guest_path = '/opt/engine-fast-io/' + digest.hexdigest()[:20]
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w') as tar:
            for file in ('bridge', 'libxcb-xtest.so.0'):
                tar.add(assets / file, arcname=file)
        command = [*manager._command(name), 'exec', name, 'sh', '-c',
                   'umask 022; mkdir -p "$1"; tar -xf - -C "$1"', 'sh', guest_path]
        result = subprocess.run(command, input=archive.getvalue(), capture_output=True, timeout=15)
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors='replace'))
        self.generation = uuid.uuid4().hex
        self.path = directory(manager, name) / (self.generation + '.frame')
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        try:
            os.ftruncate(fd, FRAME_BYTES)
        finally:
            os.close(fd)
        command = manager._command(name)
        index = next(i for i, part in enumerate(command) if part.endswith('/runsc'))
        command[index:index] = ['sh', '-c', 'exec 3<>"$1"; shift; exec "$@"', 'sh',
                               '/local/' + str(self.path.relative_to(manager.local))]
        command += ['exec', '--pass-fd=3:3', '--env=DISPLAY=:1',
                    '--env=XAUTHORITY=/home/ga/.Xauthority', '--env=LD_LIBRARY_PATH=' + guest_path,
                    name, guest_path + '/bridge']
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=None, bufsize=0, start_new_session=True)
        os.set_blocking(self.process.stdin.fileno(), False)
        self.stream = io.BufferedReader(self.process.stdout)
        try:
            self._reply()
            self.ready = True
        except BaseException:
            try:
                self.close()
            except Exception:
                pass
            raise

    def _reply(self):
        if not select.select([self.stream], [], [], 10)[0]:
            raise TimeoutError('guest I/O timed out; any submitted action has unknown delivery outcome')
        return _read_json(self.stream)

    def request(self, op, events):
        if self.broken or self.closed:
            raise RuntimeError('guest I/O channel failed; close it before reconnecting; previous action outcome is unknown')
        payload = struct.pack('<II', op, len(events)) + b''.join(EVENT.pack(*e) for e in events)
        try:
            view = memoryview(payload)
            deadline = time.monotonic() + 10
            while view:
                try:
                    written = os.write(self.process.stdin.fileno(), view)
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([], [self.process.stdin], [], remaining)[1]:
                        raise TimeoutError('guest I/O write timed out; action delivery outcome unknown') from None
                    continue
                view = view[written:]
            result = self._reply()
        except (OSError, ConnectionError, TimeoutError, ValueError):
            self.broken = True
            raise
        if 'error' in result:
            raise ValueError(result['error'])
        if not hasattr(self, 'observed_ns') or op in (1, 3):
            self.observed_ns = time.monotonic_ns()
        return {**result, 'generation': self.generation, 'frame_path': str(self.path),
                'host_frame_observed_ns': self.observed_ns,
                'backend': 'xvnc', 'acknowledgement': 'x-server-processed'}

    def close(self):
        if self.closed:
            if self.broken:
                raise RuntimeError('previous guest I/O detach failed; refusing a snapshot with uncertain shared-buffer state')
            return
        try:
            if self.process.poll() is None and not self.process.stdin.closed:
                try:
                    self.process.stdin.write(struct.pack('<II', 0, 0))
                    self.process.stdin.close()
                except BrokenPipeError:
                    pass
            code = self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.broken = True
            raise RuntimeError('guest I/O did not detach; lifecycle operation aborted') from None
        else:
            self.stream.close()
            self.process.stdin.close()
            self.path.unlink(missing_ok=True)
            self.closed = True
            self.broken = bool(code and self.ready)
            if code and self.ready:
                raise RuntimeError('guest I/O exited without a clean detach; inspect fast-io.log')

    def abort(self):
        """Only used when the caller is discarding the entire sandbox."""
        if self.closed:
            return
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=3)
        self.process.stdin.close()
        self.stream.close()
        self.path.unlink(missing_ok=True)
        self.closed = True


class FastIOClient:
    def __init__(self, name, *, manager=None, backend='xvnc'):
        if backend != 'xvnc':
            raise ValueError('only xvnc is implemented; Wayland remains deferred')
        if manager is None:
            from environment import EnvironmentManager
            manager = EnvironmentManager()
        self.manager, self.name = manager, control.valid_name(name)
        self._map = None
        self._generation = None
        self._service_seen = False
        self.last_metadata = None

    def _request(self, op, action=None):
        events = events_for_action(action) if action is not None else []
        try:
            result = _rpc(self.manager, self.name, {'op': op, 'events': events}, start=not self._service_seen)
            self._service_seen = True
            return result
        except (OSError, ConnectionError, TimeoutError) as error:
            if events:
                raise RuntimeError('action delivery outcome unknown; not retried') from error
            raise

    def action(self, action):
        return self._request('action', action)

    def _image(self, metadata):
        from PIL import Image
        if self._generation != metadata['generation']:
            self.close()
            path = Path(metadata['frame_path'])
            if path.parent != directory(self.manager, self.name) or path.suffix != '.frame':
                raise ValueError('unexpected shared frame path')
            with path.open('rb') as file:
                self._map = mmap.mmap(file.fileno(), FRAME_BYTES, access=mmap.ACCESS_READ)
            self._generation = metadata['generation']
        before = HEADER.unpack_from(self._map)
        seq, width, height, stride, _, started, completed, input_seq = before
        if seq & 1 or seq != metadata['frame_sequence']:
            return None
        if width < 1 or height < 1 or stride != width * 4 or stride * height > FRAME_BYTES - 64:
            raise ValueError('invalid shared frame dimensions')
        # PIL copies/converts BGRX to owned RGB pixels on the host, after Xvnc
        # has written directly into the shared mapping.
        pixels = memoryview(self._map)[64:64 + stride * height]
        try:
            image = Image.frombuffer('RGB', (width, height), pixels, 'raw', 'BGRX', stride, 1)
        finally:
            pixels.release()
        if HEADER.unpack_from(self._map) != before:
            return None
        self.last_metadata = {**metadata, 'capture_started_ns': started, 'capture_completed_ns': completed,
                              'frame_input_sequence': input_seq,
                              'observed_frame_age_ns': time.monotonic_ns() - metadata['host_frame_observed_ns']}
        return image

    def screenshot(self, *, fresh=True):
        for _ in range(5):
            metadata = self._request('screenshot' if fresh else 'latest')
            image = self._image(metadata)
            if image is not None:
                return image
        raise RuntimeError('concurrent capture prevented a stable frame read')

    def step(self, action):
        metadata = self._request('step', action)
        image = self._image(metadata)
        if image is None:
            # Only re-read the observation. Input is never replayed.
            image = self.screenshot()
        if self.last_metadata['generation'] != metadata['generation']:
            raise RuntimeError('environment reconnected after input; cannot return an observation from the same generation')
        return image, {**self.last_metadata, 'input_sequence': metadata['input_sequence'],
                       'action_ns': metadata['action_ns']}

    def close(self):
        if self._map is not None:
            self._map.close()
            self._map = None
        self._generation = None

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()


def serve(manager, name):
    """One host broker per environment. Serializes requests and owns the guest helper."""
    bridge = None
    stopped = False
    path = _socket_path(manager, name)
    state = manager.status(name)
    if state.get('sentry'):
        os.sched_setaffinity(0, os.sched_getaffinity(state['sentry']['pid']))
    # Prevent manual duplicate service launches, including ones bypassing the client.
    lease = (path.parent / 'service.lock').open('a+')
    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
    path.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(path)); os.chmod(path, 0o600); listener.listen(16); listener.settimeout(1)
        try:
            while not stopped:
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    if manager.status(name)['status'] in ('missing', 'stopped'):
                        break
                    continue
                with connection:
                    connection.settimeout(15)
                    pid, uid, gid = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != os.getuid():
                        continue
                    try:
                        with connection.makefile('rb') as stream:
                            request = _read_json(stream)
                        op = request['op']
                        if op == 'ping':
                            reply = {'ready': True, 'backend': 'xvnc'}
                        elif op in ('detach', 'stop'):
                            if bridge:
                                if op == 'stop' and request.get('discard'):
                                    bridge.abort()
                                else:
                                    bridge.close()
                                bridge = None
                            stopped = op == 'stop'
                            reply = {'detached': True}
                        elif op in ('action', 'screenshot', 'latest', 'step'):
                            with io_lock(manager, name):
                                state = manager.status(name)
                                if state['status'] != 'running':
                                    raise RuntimeError('fast I/O requires a running environment; resume first')
                                events = request.get('events', [])
                                if not isinstance(events, list) or len(events) > 8192 or any(
                                    not isinstance(e, list) or len(e) != 4 or any(type(v) is not int for v in e)
                                    for e in events):
                                    raise ValueError('invalid input event batch')
                                if op in ('screenshot', 'latest') and events:
                                    raise ValueError('capture request contains input')
                                if bridge is None:
                                    bridge = Bridge(manager, name)
                                reply = bridge.request({'action': 2, 'screenshot': 1, 'latest': 2, 'step': 3}[op], events)
                        else:
                            raise ValueError('unknown operation')
                    except Exception as error:
                        reply = {'error': str(error)}
                    try:
                        connection.sendall(json.dumps(reply).encode() + b'\n')
                    except OSError:
                        pass
        finally:
            try:
                if bridge:
                    bridge.close()
            finally:
                path.unlink(missing_ok=True)
                lease.close()

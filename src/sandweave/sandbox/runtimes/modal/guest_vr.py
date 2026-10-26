"""Guest-side VR stream for Modal sandboxes: Monado, its paired-eye ring and the input bridge.

Under gVisor the worker owns the frame ring and donates it to the guest. A Modal
sandbox is remote, so guest_io.py owns the same ring v2 memory here (layout
defined by scripts/vr_stream.py FrameRing and Monado's lab patch), and starts
Monado and the stdio input bridge (vr_stream_guest.py) exactly as VRStream does.
Monado's remote controllers append each vibration command they receive to
HAPTICS (r_lab_haptic_record in the lab patch); haptics() returns new records.
"""
import ctypes
import fcntl
import json
import mmap
import os
from pathlib import Path
import pwd
import select
import signal
import struct
import subprocess
import threading
import time


HEADER_BYTES = 256
SLOT_HEADER = struct.Struct('<8Q')
CONFIG = struct.Struct('<8s6I')
LOGS = Path('/run/sandweave/logs')
HAPTICS = Path('/run/sandweave/vr-haptics.bin')
# magic, controller, monotonic_ns, duration_ns, frequency, amplitude, output_name, reserved
HAPTIC = struct.Struct('<4sIQqffII')
# Per read, within the wire format's 4 MiB metadata limit; 'complete' is false when more remain.
MAX_HAPTIC_EVENTS = 8192


def read_haptics(path, after):
    """Records from index `after`; a partially visible trailing record waits for the next read."""
    with open(path, 'rb') as source:
        source.seek(after * HAPTIC.size)
        data = source.read(MAX_HAPTIC_EVENTS * HAPTIC.size)
    events = []
    for offset in range(0, len(data) - len(data) % HAPTIC.size, HAPTIC.size):
        magic, controller, guest_ns, duration_ns, frequency, amplitude, _, _ = HAPTIC.unpack_from(data, offset)
        if magic != b'HAP1' or controller not in (0, 1):
            raise ValueError(f'corrupt haptics record {after + len(events)}')
        # Monado records zero amplitude only when it ends a vibration (see the lab patch).
        events.append({'device': ('left', 'right')[controller], 'guest_ns': guest_ns, 'amplitude': amplitude,
                       'frequency_hz': frequency, 'duration_ns': duration_ns, 'stop': amplitude == 0})
    return events


class Ring:
    """Writer-shared paired-eye ring; identical layout and torn-read checks to FrameRing."""

    def __init__(self, width, height, slots, fps):
        if width * height % 16 or not (16 <= width <= 4096 and 16 <= height <= 4096 and 2 <= slots <= 64):
            raise ValueError('invalid ring dimensions or slot count')
        self.width, self.height, self.slots = width * 2, height, slots
        self.slot_bytes = 64 + self.width * height * 4
        size = HEADER_BYTES + slots * self.slot_bytes
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        self.fd = libc.syscall(319, b'vr-observations', 3)  # memfd_create, CLOEXEC | ALLOW_SEALING
        if self.fd < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        os.fchmod(self.fd, 0o600)
        os.ftruncate(self.fd, size)
        fcntl.fcntl(self.fd, 1033, 7)  # F_ADD_SEALS: SEAL | SHRINK | GROW
        self.map = mmap.mmap(self.fd, size)
        CONFIG.pack_into(self.map, 0, b'VRRING2\0', 2, self.width, height, slots, self.slot_bytes, HEADER_BYTES)
        struct.pack_into('<3Q', self.map, 32, 0, int(1e9 / fps), 1)

    def latest(self, after, timeout):
        deadline = time.monotonic() + timeout
        while True:
            sequence = struct.unpack_from('<Q', self.map, 32)[0]
            if sequence > after:
                offset = HEADER_BYTES + ((sequence - 1) % self.slots) * self.slot_bytes
                meta = SLOT_HEADER.unpack_from(self.map, offset)
                if meta[0] == sequence * 2 and meta[1] == sequence and meta[7] == 2:
                    begin = time.monotonic_ns()
                    pixels = self.map[offset + 64:offset + self.slot_bytes]
                    done = time.monotonic_ns()
                    if struct.unpack_from('<Q', self.map, offset)[0] == sequence * 2:
                        names = ('compositor_frame', 'predicted_display_ns', 'capture_begin_ns',
                                 'gpu_ready_ns', 'published_ns')
                        return {'sequence': sequence, **dict(zip(names, meta[2:7])),
                                'host_observed_ns': done, 'host_copy_ns': done - begin,
                                'width': self.width, 'height': self.height, 'eye_count': 2}, pixels
            if time.monotonic() >= deadline:
                raise TimeoutError('no complete new VR frame before deadline')
            time.sleep(.0005)


class Stream:
    def __init__(self, options):
        self.ring = Ring(options.width, options.height, options.slots, options.fps)
        self.lock = threading.Lock()
        self.buffer = bytearray()
        LOGS.mkdir(parents=True, exist_ok=True)
        Path('/run/user/1000/monado_comp_ipc').unlink(missing_ok=True)
        account = pwd.getpwnam(options.user)
        # The game's state directory (vr-monado-guest.sh): journal and game-time control file.
        self.game_dir = Path('/run/user') / str(account.pw_uid) / 'open-saber'
        self.account, self.time_lock, self.time_seq = account, threading.Lock(), time.monotonic_ns() // 1000
        descriptor = os.open(HAPTICS, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_CLOEXEC, 0o600)
        os.fchown(descriptor, account.pw_uid, account.pw_gid)
        os.close(descriptor)
        self.monado = subprocess.Popen(
            ['runuser', '-u', options.user, '--', '/usr/local/bin/engine-gpu', 'sh', '/opt/vr/vr-monado-guest.sh',
             'monado'], env={**os.environ, 'XRT_LAB_STREAM_FD': str(self.ring.fd), 'VR_HZ': str(options.hz),
                             'XRT_LAB_HAPTICS_FILE': str(HAPTICS)},
            pass_fds=(self.ring.fd,), stdin=subprocess.DEVNULL, stdout=(LOGS / 'monado.log').open('ab'),
            stderr=subprocess.STDOUT, start_new_session=True)
        self.bridge = subprocess.Popen(['runuser', '-u', options.user, '--', 'python3', '-u', '/opt/vr/vr_stream_guest.py'],
                                       cwd='/opt/vr', stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=(LOGS / 'vr-bridge.log').open('ab'), bufsize=0, start_new_session=True)
        if not self.reply(timeout=35).get('ready'):
            raise RuntimeError('VR input bridge did not initialize')

    def reply(self, timeout=5):
        deadline = time.monotonic() + timeout
        while b'\n' not in self.buffer:
            remaining = deadline - time.monotonic()
            if self.monado.poll() is not None:
                raise RuntimeError('Monado exited: ' + (LOGS / 'monado.log').read_text(errors='replace')[-2000:])
            if remaining <= 0 or not select.select([self.bridge.stdout], [], [], remaining)[0]:
                raise TimeoutError('VR input timed out; delivery outcome unknown')
            chunk = os.read(self.bridge.stdout.fileno(), 65536)
            if not chunk:
                raise ConnectionError('VR input disconnected; delivery outcome unknown')
            self.buffer.extend(chunk)
            if len(self.buffer) > 65536:
                raise ValueError('invalid VR input reply length')
        line, _, rest = self.buffer.partition(b'\n')
        self.buffer = bytearray(rest)
        return json.loads(line)

    def haptics(self, after):
        if type(after) is not int or after < 0:
            raise ValueError('haptics cursor must be a non-negative record index')
        guest_ns = time.monotonic_ns()
        events = read_haptics(HAPTICS, after)
        # Monado appends each record on receipt; every record written before guest_ns is included.
        return {'events': events, 'first': after, 'next': after + len(events), 'guest_ns': guest_ns,
                'complete': len(events) < MAX_HAPTIC_EVENTS}

    def game_time(self, scale=None, advance_ticks=None, timeout=10):
        """Pause (scale 0), run at a speed, or advance exactly N physics ticks from a pause.

        The game (SandweaveTime in the Open Saber pack) polls the control file every
        physics tick and acknowledges in <file>.ack; an advance is acknowledged again
        when its last tick completes.
        """
        if advance_ticks is not None and (type(advance_ticks) is not int or not 0 < advance_ticks <= 100_000):
            raise ValueError('advance_ticks must be a positive integer')
        if advance_ticks is None and (not isinstance(scale, (int, float)) or not 0 <= scale <= 4):
            raise ValueError('scale must be in 0..4 (0 pauses)')
        control = self.game_dir / 'time-control.json'
        if not self.game_dir.is_dir():
            raise RuntimeError('the game provides no time control (' + str(self.game_dir) + ' is missing)')
        with self.time_lock:
            self.time_seq += 1
            request = {'seq': self.time_seq, 'scale': 1.0 if scale is None else float(scale),
                       'advance_ticks': advance_ticks or 0}
            temporary = control.with_name('.time-control.tmp')
            temporary.write_text(json.dumps(request))
            os.chown(temporary, self.account.pw_uid, self.account.pw_gid)
            temporary.chmod(0o644)
            sent = time.monotonic_ns()
            temporary.replace(control)
            wanted = 'advanced' if advance_ticks else 'applied'
            deadline = time.monotonic() + timeout
            while True:
                try:
                    ack = json.loads((self.game_dir / 'time-control.json.ack').read_text())
                except (FileNotFoundError, ValueError):
                    ack = {}
                if ack.get('seq') == self.time_seq and ack.get('state') == wanted:
                    return {**ack, 'requested': request, 'guest_sent_ns': sent, 'guest_ns': time.monotonic_ns()}
                if self.monado.poll() is not None or time.monotonic() > deadline:
                    raise TimeoutError('the game did not acknowledge the time request: ' + json.dumps(request))
                time.sleep(.001)

    def request(self, message):
        payload = json.dumps(message, separators=(',', ':'), allow_nan=False).encode() + b'\n'
        if len(payload) > 65536:
            raise ValueError('VR input message exceeds 64 KiB')
        with self.lock:
            self.bridge.stdin.write(payload)
            return self.reply()

    def close(self):
        if self.bridge.poll() is None:
            self.bridge.stdin.close()
            try:
                self.bridge.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.bridge.kill()
        if self.monado.poll() is None:
            os.killpg(self.monado.pid, signal.SIGTERM)
            try:
                self.monado.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(self.monado.pid, signal.SIGKILL)
                self.monado.wait()

"""Guest-side VR stream for Modal sandboxes: Monado, its paired-eye ring and the input bridge.

Under gVisor the worker owns the frame ring and donates it to the guest. A Modal
sandbox is remote, so guest_io.py owns the same ring v2 memory here (layout
defined by scripts/vr_stream.py FrameRing and Monado's lab patch), and starts
Monado and the stdio input bridge (vr_stream_guest.py) exactly as VRStream does.
"""
import ctypes
import fcntl
import json
import mmap
import os
from pathlib import Path
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
        self.monado = subprocess.Popen(
            ['runuser', '-u', options.user, '--', '/usr/local/bin/engine-gpu', 'sh', '/opt/vr/vr-monado-guest.sh',
             'monado'], env={**os.environ, 'XRT_LAB_STREAM_FD': str(self.ring.fd), 'VR_HZ': str(options.hz)},
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

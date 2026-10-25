"""Guest-side VR stream for Modal sandboxes: Monado, its paired-eye ring and the input bridge.

Under gVisor the worker owns the frame ring and donates it to the guest. A Modal
sandbox is remote, so this server owns the same ring v2 memory (layout defined by
scripts/vr_stream.py FrameRing and Monado's lab patch), starts Monado and the
stdio input bridge (vr_stream_guest.py) exactly as VRStream does, and serves
frames and input over an authenticated HTTP endpoint using Sandweave's wire format.
Run as guest root: python3 guest_vr.py PORT TOKEN_FILE [--width ...].
"""
import argparse
import ctypes
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import mmap
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sandweave_wire import MAX_BODY, decode, encode  # noqa: E402

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('port', type=int)
    parser.add_argument('token_file')
    parser.add_argument('--width', type=int, default=960)
    parser.add_argument('--height', type=int, default=1080)
    parser.add_argument('--fps', type=int, default=90)
    parser.add_argument('--hz', type=int, default=90)
    parser.add_argument('--slots', type=int, default=8)
    parser.add_argument('--user', default='ga')
    options = parser.parse_args()
    token = Path(options.token_file).read_text().strip()
    import zstandard
    stream = Stream(options)
    local = threading.local()

    def call(op, parameters):
        if op == 'ping':
            return {'guest_ns': time.monotonic_ns()}
        if op == 'latest':
            timeout = parameters.get('timeout', 5)
            if not 0 < timeout <= 120:
                raise ValueError('timeout must be in 0..120 seconds')
            frame, pixels = stream.ring.latest(parameters.get('after', 0), timeout)
            if not hasattr(local, 'compressor'):
                local.compressor = zstandard.ZstdCompressor(level=1)
            return {**frame, 'rgba': local.compressor.compress(pixels), 'codec': 'zstd'}
        if op == 'input':
            reply = stream.request(parameters['request'])
            return {**reply, 'guest_reply_ns': time.monotonic_ns()}
        if op == 'status':
            return {'monado': stream.monado.poll(), 'bridge': stream.bridge.poll()}
        if op == 'close':
            stream.close()
            threading.Thread(target=server.shutdown, daemon=True).start()
            return {}
        raise ValueError('unknown VR operation')

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get('X-Sandweave-Token', ''), token):
                self.send_error(403)
                return
            try:
                length = int(self.headers.get('Content-Length', '-1'))
                if not 0 <= length <= MAX_BODY:
                    raise ValueError('invalid request size')
                request = decode(self.rfile.read(length))
                result = {'result': call(request['op'], request.get('params', {}))}
            except Exception as error:
                result = {'error': {'kind': type(error).__name__, 'message': str(error)}}
            payload = encode(result)
            self.send_response(200)
            self.send_header('Content-Type', 'application/vnd.sandweave.frame')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(('0.0.0.0', options.port), Handler)
    server.daemon_threads = True
    signal.signal(signal.SIGTERM, lambda *args: threading.Thread(target=server.shutdown, daemon=True).start())
    try:
        server.serve_forever(poll_interval=.1)
    finally:
        stream.close()


if __name__ == '__main__':
    main()

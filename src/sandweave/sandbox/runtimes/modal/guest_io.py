"""Guest-side fast I/O relay for Modal sandboxes, run as guest root.

Under gVisor the worker shares the fast I/O frame buffer and VR ring with the
guest. A Modal sandbox is remote, so this relay owns them inside the guest:
the qualified fast I/O bridge (desktop input, X frames), the VR stream
(Monado ring, input bridge and haptics record, from guest_vr.py) and audio
capture (guest_audio.py). It serves one persistent,
authenticated, length-prefixed connection per client, encoding frames in the
guest (JPEG, PNG or zstd-compressed raw) so only compact bytes cross the tunnel.

    python3 guest_io.py PORT TOKEN_FILE
"""
import hmac
import io
import json
import mmap
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import threading
import time
import types

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sandweave_wire import decode, encode  # noqa: E402

FRAME_BYTES = 64 + 64 * 1024 * 1024
HEADER = struct.Struct('<QIIIIQQQ')
EVENT = struct.Struct('<IIii')
BRIDGE_OPS = {'action': 2, 'screenshot': 1, 'screenshot_cursor': 5, 'latest': 2, 'step': 3}
FAST_IO = '/opt/engine-fast-io'
LENGTH = struct.Struct('<I')
MAX_MESSAGE = 64 * 1024**2


def encode_image(mode, size, pixels, raw_mode, stride, image_format, quality):
    if image_format == 'zstd':
        import zstandard
        return zstandard.ZstdCompressor(level=1).compress(bytes(pixels))
    from PIL import Image
    image = Image.frombuffer(mode, size, pixels, 'raw', raw_mode, stride, 1)
    output = io.BytesIO()
    if image_format == 'jpeg':
        image.save(output, 'JPEG', quality=quality)
    else:
        image.save(output, 'PNG', compress_level=1)
    return output.getvalue()


class Desktop:
    """The fast I/O bridge with a guest-local frame buffer (identical protocol to fast_io.Bridge)."""

    def __init__(self, display=':1', authority='/home/ga/.Xauthority'):
        self.lock = threading.Lock()
        self.path = Path('/run/sandweave/fast-io.frame')
        fd = os.open(self.path, os.O_CREAT | os.O_TRUNC | os.O_RDWR, 0o600)
        os.ftruncate(fd, FRAME_BYTES)
        self.map = mmap.mmap(fd, FRAME_BYTES, access=mmap.ACCESS_READ)
        self.process = subprocess.Popen(
            [FAST_IO + '/bridge'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, bufsize=0,
            pass_fds=(fd,), preexec_fn=lambda: os.dup2(fd, 3),
            env={**os.environ, 'DISPLAY': display, 'XAUTHORITY': authority, 'LD_LIBRARY_PATH': FAST_IO})
        os.close(fd)
        self.stream = io.BufferedReader(self.process.stdout)
        self.capabilities = self._reply()

    def _reply(self):
        if not select.select([self.stream], [], [], 10)[0]:
            raise TimeoutError('fast I/O bridge timed out')
        line = self.stream.readline(1024 * 1024)
        if not line:
            raise ConnectionError('fast I/O bridge exited')
        return json.loads(line)

    def request(self, op, events, image_format=None, quality=85):
        with self.lock:
            payload = struct.pack('<II', BRIDGE_OPS[op], len(events)) + b''.join(EVENT.pack(*e) for e in events)
            self.process.stdin.write(payload)
            reply = self._reply()
            if 'error' in reply:
                raise ValueError(reply['error'])
            if op == 'action' or image_format is None:
                return reply
            for _ in range(5):
                before = HEADER.unpack_from(self.map)
                sequence, width, height, stride, _, started, completed, input_sequence = before
                if sequence & 1 or sequence != reply.get('frame_sequence', sequence):
                    reply = self._reply_after(2)
                    continue
                pixels = memoryview(self.map)[64:64 + stride * height]
                try:
                    data = encode_image('RGB', (width, height), pixels, 'BGRX', stride, image_format, quality)
                finally:
                    pixels.release()
                if HEADER.unpack_from(self.map) == before:
                    return {**reply, 'image': data, 'format': image_format, 'size': [width, height],
                            'capture_started_ns': started, 'capture_completed_ns': completed,
                            'frame_input_sequence': input_sequence}
                reply = self._reply_after(2)
            raise RuntimeError('concurrent capture prevented a stable frame read')

    def _reply_after(self, op):
        self.process.stdin.write(struct.pack('<II', op, 0))
        return self._reply()


def serve_client(connection, token, state):
    def receive():
        header = b''
        while len(header) < LENGTH.size:
            chunk = connection.recv(LENGTH.size - len(header))
            if not chunk:
                return None
            header += chunk
        (size,) = LENGTH.unpack(header)
        if size > MAX_MESSAGE:
            raise ValueError('message too large')
        data = bytearray()
        while len(data) < size:
            chunk = connection.recv(min(1 << 20, size - len(data)))
            if not chunk:
                raise ConnectionError('client disconnected mid-message')
            data += chunk
        return decode(bytes(data))

    def send(value):
        payload = encode(value)
        connection.sendall(LENGTH.pack(len(payload)) + payload)

    connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    hello = receive()
    if not hello or not hmac.compare_digest(str(hello.get('token', '')), token):
        return
    send({'ready': True, 'guest_ns': time.monotonic_ns()})
    while (request := receive()) is not None:
        try:
            result = call(state, request.get('op'), request.get('params', {}))
            send({'id': request.get('id'), 'result': result})
        except Exception as error:
            send({'id': request.get('id'), 'error': {'kind': type(error).__name__, 'message': str(error)}})


def call(state, op, parameters):
    if op == 'ping':
        return {'guest_ns': time.monotonic_ns()}
    if op == 'desktop':
        with state.lock:
            if state.desktop is None:
                state.desktop = Desktop()
        events = [tuple(event) for event in parameters.get('events', [])]
        return {**state.desktop.request(parameters['op'], events, parameters.get('format'),
                                        parameters.get('quality', 85)), 'guest_ns': time.monotonic_ns()}
    if op == 'vr_start':
        from guest_vr import Stream
        with state.lock:
            if state.vr is None:
                state.vr = Stream(types.SimpleNamespace(
                    width=parameters.get('width', 960), height=parameters.get('height', 1080),
                    fps=parameters.get('fps', 90), hz=parameters.get('hz', 90), slots=parameters.get('slots', 8),
                    user=parameters.get('user', 'ga')))
        return {'started': True}
    if op == 'vr_latest':
        frame, pixels = state.vr.ring.latest(parameters.get('after', 0), parameters.get('timeout', 5))
        image_format = parameters.get('format', 'jpeg')
        data = encode_image('RGB', (frame['width'], frame['height']), pixels, 'RGBX', frame['width'] * 4,
                            image_format, parameters.get('quality', 90))
        return {**frame, 'image': data, 'format': image_format}
    if op == 'vr_input':
        return {**state.vr.request(parameters['request']), 'guest_reply_ns': time.monotonic_ns()}
    if op == 'vr_haptics':
        if state.vr is None:
            raise RuntimeError('the VR stream has not started')
        return state.vr.haptics(parameters.get('after', 0))
    if op == 'game_time':
        if state.vr is None:
            raise RuntimeError('the VR stream has not started')
        return state.vr.game_time(parameters.get('scale'), parameters.get('advance_ticks'),
                                  parameters.get('timeout', 10))
    if op == 'audio_start':
        from guest_audio import Capture
        with state.lock:
            if state.audio is None:
                state.audio = Capture(parameters.get('user', 'ga'))
        return {'started': True, 'guest_ns': time.monotonic_ns()}
    if op == 'audio_read':
        if state.audio is None:
            raise RuntimeError('audio capture has not started')
        return state.audio.read(parameters.get('after', 0))
    if op == 'vr_close':
        with state.lock:
            if state.vr is not None:
                state.vr.close()
                state.vr = None
        return {}
    raise ValueError('unknown I/O operation: ' + str(op))


def main():
    port, token = int(sys.argv[1]), Path(sys.argv[2]).read_text().strip()
    state = types.SimpleNamespace(lock=threading.Lock(), desktop=None, vr=None, audio=None)
    server = socket.create_server(('0.0.0.0', port), reuse_port=False)
    while True:
        connection, _ = server.accept()
        threading.Thread(target=lambda c=connection: (serve_client(c, token, state), c.close()), daemon=True).start()


if __name__ == '__main__':
    main()

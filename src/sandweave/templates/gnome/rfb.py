"""A read-only RFB 3.8 capture connection to the worker's loopback Xvnc.

RFC 6143: https://www.rfc-editor.org/rfc/rfc6143.html
Omitting Cursor pseudo-encoding asks Xvnc to render the real pointer in its
framebuffer updates. Cursor-free recording uses the screenshot backend instead.
No input or clipboard updates are sent, and the shared flag preserves viewers.
"""
import importlib.util
import socket
import struct
import time
import zlib


class Capture:
    def __init__(self, port, password, *, cursor=True):
        if not cursor:
            raise ValueError('cursor-free recording uses PlainCapture')
        from PIL import Image
        from Crypto.Cipher import DES
        from ...sandbox.workspace import engine_sources
        spec = importlib.util.spec_from_file_location('_sandweave_zrle', engine_sources() / 'vnc_zrle.py')
        decoder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(decoder)
        self.decode = decoder.decode
        self.deadline = None
        self.socket = socket.create_connection(('127.0.0.1', port), timeout=5)
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.zlib = zlib.decompressobj()
        try:
            banner = self.read(12)
            if banner != b'RFB 003.008\n':
                raise ValueError('desktop recording requires Xvnc with RFB 3.8')
            self.socket.sendall(banner)
            count = self.read(1)[0]
            if not count or 2 not in self.read(count):
                raise ValueError('Xvnc does not offer VNC password authentication')
            self.socket.sendall(b'\x02')
            key = bytes(int(f'{b:08b}'[::-1], 2) for b in password[:8].ljust(8, b'\0'))
            self.socket.sendall(DES.new(key, DES.MODE_ECB).encrypt(self.read(16)))
            if self.number():
                raise ValueError('Xvnc rejected the recording connection')
            self.socket.sendall(b'\x01')  # Shared viewer; never disconnect another client.
            self.width, self.height = struct.unpack('>HH', self.read(4))
            self.read(16)
            self.read(self.number(limit=1024**2))
            self._dimensions()
            self.image = Image.new('RGB', (self.width, self.height))
            # Little-endian RGB32, packed BGR pixels in ZRLE.
            pixel = struct.pack('>BBBBHHHBBB3x', 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
            self.socket.sendall(b'\0\0\0\0' + pixel)
            # Compression reduces traffic through the guest's network relay.
            encodings = [16, 0, -223]
            self.socket.sendall(struct.pack('>BBH', 2, 0, len(encodings)) +
                                b''.join(struct.pack('>i', e) for e in encodings))
        except BaseException:
            self.close()
            raise

    def read(self, size):
        data = bytearray()
        while len(data) < size:
            if self.deadline is not None:
                remaining = self.deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('desktop capture timed out')
                self.socket.settimeout(remaining)
            part = self.socket.recv(min(size-len(data), 1024**2))
            if not part:
                raise ConnectionError('desktop display disconnected')
            data.extend(part)
        return bytes(data)

    def number(self, *, limit=None):
        value = struct.unpack('>I', self.read(4))[0]
        if limit is not None and value > limit:
            raise ValueError('RFB message exceeds its size limit')
        return value

    def _dimensions(self):
        if not (1 <= self.width <= 3840 and 1 <= self.height <= 2160):
            raise ValueError('recording display size exceeds 3840x2160')

    def frame(self):
        from PIL import Image
        started = time.monotonic_ns()
        self.deadline = time.monotonic()+5
        self.socket.sendall(struct.pack('>BBHHHH', 3, 0, 0, 0, self.width, self.height))
        while True:
            kind = self.read(1)[0]
            if kind == 2:  # Bell.
                continue
            if kind == 3:  # Ignore server clipboard, including its contents.
                self.read(3)
                self.read(self.number(limit=4*1024**2))
                continue
            if kind != 0:
                raise ValueError('unexpected RFB server message')
            _, count = struct.unpack('>BH', self.read(3))
            pixels_received = False
            for _ in range(count):
                x, y, width, height, encoding = struct.unpack('>HHHHi', self.read(12))
                if encoding == -223:
                    self.width, self.height = width, height
                    self._dimensions()
                    self.image = Image.new('RGB', (width, height))
                    continue
                if not width or not height or x+width > self.width or y+height > self.height:
                    raise ValueError('RFB rectangle is outside the display')
                if encoding == 0:
                    pixels = self.read(width * height * 4)
                    self.image.paste(Image.frombytes('RGB', (width, height), pixels, 'raw', 'BGRX'), (x, y))
                    pixels_received = True
                elif encoding == 16:
                    packed = self.read(self.number(limit=64*1024**2))
                    decoded = self.zlib.decompress(packed, 64*1024**2)
                    if self.zlib.unconsumed_tail:
                        raise ValueError('RFB decompression exceeds size limit')
                    for tx, ty, w, h, pixels in self.decode(decoded, width, height):
                        self.image.paste(Image.frombytes('RGB', (w, h), pixels, 'raw', 'BGRX'), (x+tx, y+ty))
                    pixels_received = True
                else:
                    raise ValueError('unexpected RFB encoding: ' + str(encoding))
            if pixels_received:
                self.deadline = None
                return self.image.copy(), {'capture_started_ns': started,
                    'capture_completed_ns': time.monotonic_ns(),
                    'clock': 'worker CLOCK_MONOTONIC',
                    'meaning': 'display request/response interval; not application render time'}
            self.socket.sendall(struct.pack('>BBHHHH', 3, 0, 0, 0, self.width, self.height))

    def close(self):
        self.socket.close()

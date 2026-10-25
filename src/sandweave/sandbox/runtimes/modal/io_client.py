"""Worker side of guest_io.py: one persistent TLS connection with pipelined, ID-matched replies."""
import itertools
import socket
import ssl
import struct
import threading

from ...wire import decode, encode

LENGTH = struct.Struct('<I')


class IOConnection:
    def __init__(self, host, port, token, *, timeout=30):
        raw = socket.create_connection((host, port), timeout=timeout)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.socket = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        self.ids = itertools.count(1)
        self.send_lock, self.receive_lock = threading.Lock(), threading.Lock()
        self.replies, self.waiting = {}, threading.Condition()
        self.closed = False
        self._send({'token': token})
        if not self._receive().get('ready'):
            raise ConnectionError('guest I/O relay rejected the connection')

    def _send(self, value):
        payload = encode(value)
        with self.send_lock:
            self.socket.sendall(LENGTH.pack(len(payload)) + payload)

    def _read(self, size):
        data = bytearray()
        while len(data) < size:
            chunk = self.socket.recv(min(1 << 20, size - len(data)))
            if not chunk:
                raise ConnectionError('guest I/O relay disconnected')
            data += chunk
        return bytes(data)

    def _receive(self):
        (size,) = LENGTH.unpack(self._read(LENGTH.size))
        return decode(self._read(size))

    def call(self, op, **parameters):
        """Send a request and wait for its reply; concurrent callers pipeline over one connection."""
        if self.closed:
            raise ConnectionError('guest I/O connection is closed')
        identity = next(self.ids)
        self._send({'id': identity, 'op': op, 'params': parameters})
        while True:
            with self.waiting:
                if identity in self.replies:
                    reply = self.replies.pop(identity)
                    break
            if self.receive_lock.acquire(timeout=.001):
                try:
                    message = self._receive()
                finally:
                    self.receive_lock.release()
                with self.waiting:
                    self.replies[message.get('id')] = message
                    self.waiting.notify_all()
            else:
                with self.waiting:
                    self.waiting.wait(.01)
        if 'error' in reply:
            raise RuntimeError(f"{reply['error']['kind']}: {reply['error']['message']}")
        return reply['result']

    def close(self):
        self.closed = True
        try:
            self.socket.close()
        except OSError:
            pass

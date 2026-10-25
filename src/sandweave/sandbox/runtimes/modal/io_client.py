"""Worker side of guest_io.py: one persistent TLS connection, one request at a time.

An SSL socket must not be read and written from different threads at once, so
each connection serializes request/reply pairs; callers needing concurrency
(for example frames and VR input) use separate connections.
"""
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
        self.ids, self.lock = itertools.count(1), threading.Lock()
        self.closed = False
        self._send({'token': token})
        if not self._receive().get('ready'):
            raise ConnectionError('guest I/O relay rejected the connection')

    def _send(self, value):
        payload = encode(value)
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
        with self.lock:
            if self.closed:
                raise ConnectionError('guest I/O connection is closed')
            identity = next(self.ids)
            try:
                self._send({'id': identity, 'op': op, 'params': parameters})
                reply = self._receive()
            except BaseException:
                self.closed = True
                raise
        if reply.get('id') != identity:
            raise ConnectionError('guest I/O reply does not match its request')
        if 'error' in reply:
            raise RuntimeError(f"{reply['error']['kind']}: {reply['error']['message']}")
        return reply['result']

    def close(self):
        self.closed = True
        try:
            self.socket.close()
        except OSError:
            pass

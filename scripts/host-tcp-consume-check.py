#!/usr/bin/env python3
"""Check host TCP receive semantics used by passt's host-to-guest forwarding."""
import ctypes
import ctypes.util
import json
import os
import socket
import time

MSG_PEEK, MSG_TRUNC, MSG_DONTWAIT = 0x2, 0x20, 0x40
libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
libc.recv.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
libc.recv.restype = ctypes.c_ssize_t


class IOVec(ctypes.Structure):
    _fields_ = [('base', ctypes.c_void_p), ('len', ctypes.c_size_t)]


class MsgHdr(ctypes.Structure):
    _fields_ = [('name', ctypes.c_void_p), ('namelen', ctypes.c_uint32), ('iov', ctypes.POINTER(IOVec)),
                ('iovlen', ctypes.c_size_t), ('control', ctypes.c_void_p), ('controllen', ctypes.c_size_t),
                ('flags', ctypes.c_int)]


libc.recvmsg.argtypes = [ctypes.c_int, ctypes.POINTER(MsgHdr), ctypes.c_int]
libc.recvmsg.restype = ctypes.c_ssize_t


def pair():
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    client = socket.create_connection(listener.getsockname())
    server, _ = listener.accept()
    listener.close()
    return client, server


def wait_readable(sock, size):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if len(sock.recv(size, socket.MSG_PEEK | socket.MSG_DONTWAIT)) >= size:
            return
        time.sleep(.01)
    raise TimeoutError('loopback data did not arrive')


def peek_after(sock, skip, size):
    """passt without SO_PEEK_OFF: peek into a discard buffer, then the payload buffer."""
    discard, payload = ctypes.create_string_buffer(max(skip, 1)), ctypes.create_string_buffer(size)
    iov = (IOVec * 2)(IOVec(ctypes.cast(discard, ctypes.c_void_p), skip),
                      IOVec(ctypes.cast(payload, ctypes.c_void_p), size))
    header = MsgHdr(None, 0, iov, 2, None, 0, 0)
    count = libc.recvmsg(sock.fileno(), ctypes.byref(header), MSG_PEEK | MSG_DONTWAIT)
    if count < 0:
        return {'error': os.strerror(ctypes.get_errno())}
    return {'returned': count, 'payload': payload.raw[:max(0, count - skip)].decode(errors='replace')}


def main():
    data = bytes(range(48, 48 + 64))  # printable, position-identifying bytes
    client, server = pair()
    client.sendall(data)
    wait_readable(server, len(data))
    results = {'peek_after_10': peek_after(server, 10, 20)}
    count = libc.recv(server.fileno(), None, 10, MSG_DONTWAIT | MSG_TRUNC)
    results['trunc_null_10'] = {'returned': count, 'errno': os.strerror(ctypes.get_errno()) if count < 0 else None}
    following = server.recv(10)
    results['next_after_trunc'] = following.decode()
    results['trunc_discarded'] = following == data[10:20]
    results['peek_after_trunc_5'] = peek_after(server, 5, 10)
    results['peek_expected'] = data[25:35].decode()
    client.close(); server.close()
    print(json.dumps(results, indent=2))
    raise SystemExit(0 if results['trunc_discarded'] and
                     results['peek_after_trunc_5'].get('payload') == results['peek_expected'] else 1)


if __name__ == '__main__':
    main()

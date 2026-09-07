#!/usr/bin/env python3
"""Relay the legacy virgl vtest stream across the UML/host kernel boundary."""
import argparse
import json
import socket
import socketserver
import struct
import threading
import time

p = argparse.ArgumentParser()
listen = p.add_mutually_exclusive_group(required=True)
listen.add_argument('--listen-tcp', type=int)
listen.add_argument('--listen-unix')
upstream = p.add_mutually_exclusive_group(required=True)
upstream.add_argument('--upstream-tcp')
upstream.add_argument('--upstream-unix')
p.add_argument('--force-legacy', action='store_true')
a = p.parse_args()


def exact(sock, length):
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise EOFError('Connection closed during protocol negotiation')
        data.extend(chunk)
    return bytes(data)


def nodelay(sock):
    if sock.family == socket.AF_INET:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        start = time.monotonic()
        incoming = self.request
        nodelay(incoming)
        if a.upstream_unix:
            outgoing = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            outgoing.connect(a.upstream_unix)
        else:
            host, port = a.upstream_tcp.rsplit(':', 1)
            outgoing = socket.create_connection((host, int(port)), timeout=15)
            outgoing.settimeout(None)
        nodelay(outgoing)
        counts = [0, 0]
        errors = []

        def forward(source, destination, direction, negotiate=False):
            try:
                if negotiate:
                    # Mesa23.2.1 starts with CREATE_RENDERER, PING, BUSY_WAIT,
                    # then PROTOCOL_VERSION. CREATE_RENDERER's length is bytes;
                    # the other message lengths are 32-bit words.
                    while True:
                        header = exact(source, 8)
                        length, command = struct.unpack('<II', header)
                        if command not in (8, 10, 7, 11) or length > 262144:
                            raise ValueError(f'Unexpected negotiation header {length}, {command}')
                        size = length if command == 8 else length * 4
                        payload = exact(source, size)
                        if command == 11:
                            if size != 4:
                                raise ValueError('Invalid protocol-version payload')
                            requested, = struct.unpack('<I', payload)
                            payload = struct.pack('<I', 0)
                            print(json.dumps({'event': 'force_protocol', 'requested': requested,
                                              'forwarded': 0}), flush=True)
                        destination.sendall(header + payload)
                        counts[direction] += len(header) + len(payload)
                        if command == 11:
                            break
                while True:
                    data = source.recv(65536)
                    if not data:
                        break
                    destination.sendall(data)
                    counts[direction] += len(data)
            except (OSError, EOFError, ValueError) as error:
                errors.append(str(error))
            finally:
                try:
                    destination.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        downstream = threading.Thread(target=forward, args=(outgoing, incoming, 1), daemon=True)
        downstream.start()
        forward(incoming, outgoing, 0, a.force_legacy)
        downstream.join(timeout=5)
        outgoing.close()
        print(json.dumps({'event': 'connection_closed', 'seconds': time.monotonic() - start,
                          'client_to_renderer_bytes': counts[0],
                          'renderer_to_client_bytes': counts[1], 'errors': errors}), flush=True)


class TCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


server_class = TCPServer if a.listen_tcp else UnixServer
address = ('127.0.0.1', a.listen_tcp) if a.listen_tcp else a.listen_unix
with server_class(address, Handler) as server:
    print(json.dumps({'event': 'listening', 'address': address,
                      'force_legacy': a.force_legacy}), flush=True)
    server.serve_forever()

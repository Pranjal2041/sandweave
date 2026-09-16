#!/usr/bin/env python3
"""Relay complete Ethernet messages to passt's length-prefixed stream."""
import argparse
import socket
import struct
import threading
import json
import socketserver
from pathlib import Path

from network_policy import NetworkPolicy
import _unix_sockets

MAX_FRAME = 9014


class PolicyServer(socketserver.UnixStreamServer):
    """Worker-only control socket; packet forwarding never polls the filesystem."""
    def __init__(self, path, policy, config):
        self.policy, self.config = policy, config
        with _unix_sockets.Address(path) as address:
            super().__init__(address, PolicyRequest)


class PolicyRequest(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        try:
            raw = self.rfile.readline(65537)
            if len(raw) > 65536 or not raw.endswith(b'\n'):
                raise ValueError('invalid network policy request')
            changes = json.loads(raw)
            if not isinstance(changes, dict) or set(changes) - {'mode', 'allow_cidrs', 'proxy_endpoints', 'allowed_hosts'}:
                raise ValueError('invalid network policy fields')
            config = {**self.server.config, **changes}
            self.server.policy.update(config)
            self.server.config = config
            result = {'ok': True}
        except Exception as error:
            result = {'error': str(error)}
        self.wfile.write(json.dumps(result).encode() + b'\n')


def read_exact(sock, count):
    data = bytearray()
    while len(data) < count:
        part = sock.recv(count - len(data))
        if not part:
            if data:
                raise ValueError('truncated passt frame')
            raise EOFError
        data.extend(part)
    return bytes(data)


def relay(packet, stream, policy=None, peer=None):
    errors = []
    finished = threading.Event()

    def pump(direction):
        try:
            while True:
                if direction == 'to-passt':
                    frame, _, flags, _ = packet.recvmsg(MAX_FRAME)
                    if not frame:
                        return
                    if flags & socket.MSG_TRUNC:
                        raise ValueError('oversized Ethernet message')
                else:
                    length, = struct.unpack('!I', read_exact(stream, 4))
                    if not 14 <= length <= MAX_FRAME:
                        raise ValueError(f'invalid Ethernet length: {length}')
                    frame = read_exact(stream, length)
                if not 14 <= len(frame) <= MAX_FRAME:
                    raise ValueError(f'invalid Ethernet frame: {len(frame)}')
                if direction == 'to-passt' and peer is not None and peer.send(frame):
                    continue
                if policy is not None and not policy.allow(frame, direction):
                    continue
                if direction == 'to-passt':
                    stream.sendall(struct.pack('!I', len(frame)) + frame)
                elif packet.send(frame) != len(frame):
                    raise OSError('short SOCK_SEQPACKET send')
        except EOFError:
            pass
        except (OSError, ValueError) as exc:
            if not finished.is_set():
                errors.append(exc)
        finally:
            finished.set()
            for sock in (packet, stream):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    thread = threading.Thread(target=pump, args=('to-passt',))
    thread.start()
    pump('from-passt')
    thread.join()
    if errors:
        raise errors[0]


def main():
    import os
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--listen', required=True)
    parser.add_argument('--passt', required=True)
    parser.add_argument('--policy', required=True)
    parser.add_argument('--service-network')
    args = parser.parse_args()
    config = json.loads(Path(args.policy).read_text())
    policy = NetworkPolicy(config)
    os.umask(0o077)
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
        _unix_sockets.bind(listener, args.listen)
        try:
            listener.listen(1)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                _unix_sockets.connect(stream, args.passt)
                packet, _ = listener.accept()
                control = args.listen + '.control'
                try:
                    with PolicyServer(control, policy, config) as server:
                        thread = threading.Thread(target=server.serve_forever, daemon=True)
                        thread.start()
                        peer = None
                        try:
                            if args.service_network:
                                from service_network import Peer
                                peer = Peer(json.loads(Path(args.service_network).read_text()), packet)
                            with packet:
                                relay(packet, stream, policy, peer)
                        finally:
                            if peer is not None:
                                peer.close()
                            server.shutdown()
                            thread.join()
                finally:
                    Path(control).unlink(missing_ok=True)
        finally:
            print(json.dumps(dict(policy.counts), sort_keys=True), flush=True)
            os.unlink(args.listen)


if __name__ == '__main__':
    main()

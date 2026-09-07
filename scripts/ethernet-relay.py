#!/usr/bin/env python3
"""Relay complete Ethernet messages to passt's length-prefixed stream."""
import argparse
import socket
import struct
import threading
import json
from pathlib import Path

from network_policy import NetworkPolicy

MAX_FRAME = 9014


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


def relay(packet, stream, policy=None):
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
    args = parser.parse_args()
    policy = NetworkPolicy(json.loads(Path(args.policy).read_text()))
    os.umask(0o077)
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
        listener.bind(args.listen)
        try:
            listener.listen(1)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.connect(args.passt)
                packet, _ = listener.accept()
                with packet:
                    relay(packet, stream, policy)
        finally:
            print(json.dumps(dict(policy.counts), sort_keys=True), flush=True)
            os.unlink(args.listen)


if __name__ == '__main__':
    main()

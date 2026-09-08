#!/usr/bin/env python3
"""Persistent stdio-to-Monado input bridge, run inside the sandbox only."""
import json
import socket
import sys
import time

from vr_input import ACK_MAGIC, MAGIC, receive, update


def reply(value):
    print(json.dumps(value, separators=(',', ':'), allow_nan=False), flush=True)


def main():
    deadline = time.monotonic() + 30
    while True:
        try:
            sock = socket.create_connection(('127.0.0.1', 4242), timeout=3)
            break
        except ConnectionRefusedError:
            if time.monotonic() > deadline:
                raise
            time.sleep(.05)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    with sock:
        reset, current = receive(sock), receive(sock)
        current = update(current, {'left': {'hand_tracking_active': False},
                                   'right': {'hand_tracking_active': False}})
        reply({'ready': True, 'guest_ns': time.monotonic_ns()})
        try:
            while True:
                line = sys.stdin.buffer.readline(65537)
                if not line:
                    break
                if len(line) > 65536 or not line.endswith(b'\n'):
                    raise ValueError('invalid input message length')
                request = json.loads(line)
                received = time.monotonic_ns()
                try:
                    if request.get('op') == 'ping':
                        reply({'guest_ns': received})
                        continue
                    if request.get('op') != 'input':
                        raise ValueError('unknown operation')
                    next_state = update(current, request['state'])
                except (KeyError, ValueError) as error:
                    reply({'error': str(error)})
                    continue
                next_state.header = ACK_MAGIC
                sock.sendall(bytes(next_state))
                observed = receive(sock)
                next_state.header = MAGIC
                if bytes(observed) != bytes(next_state):
                    raise ConnectionError('Monado acknowledged a different state')
                current = observed
                reply({'id': request['id'], 'guest_received_ns': received,
                       'guest_ack_ns': time.monotonic_ns(),
                       'acknowledgement': 'monado-state-received'})
        finally:
            # Do not leave held buttons/axes after a normal EOF or protocol error.
            release = {}
            from vr_input import BUTTONS, SCALARS
            for side in ('left', 'right'):
                release[side] = {key: False for key in BUTTONS if key not in ('active', 'hand_tracking_active')}
                release[side].update({key: 0 for key in SCALARS})
                release[side].update(thumbstick=[0, 0], trackpad=[0, 0],
                                     linear_velocity=[0, 0, 0], angular_velocity=[0, 0, 0])
            current = update(current, release)
            current.header = ACK_MAGIC
            try:
                sock.sendall(bytes(current))
                receive(sock)
            except (OSError, ValueError, ConnectionError):
                pass


if __name__ == '__main__':
    main()

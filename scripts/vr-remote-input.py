#!/usr/bin/env python3
"""Control Monado's remote HMD/Index controllers from inside the sandbox.

Wire layout matches r_interface.h at f8dfadfeaeb46df3eec17bd76b7abdf42a79108c
on little-endian x86_64. This is the runtime's pose/input protocol, not the
game's desktop simulator. The protocol has no per-action acknowledgement.
"""
import argparse
import ctypes as C
import json
import math
import socket
import sys
import time


class Pose(C.Structure):
    _fields_ = [('orientation', C.c_float * 4), ('position', C.c_float * 3)]


class View(C.Structure):
    _fields_ = [('fov', C.c_float * 4), ('pose', Pose), ('pad', C.c_uint32)]


class Head(C.Structure):
    _fields_ = [('views', View * 2), ('center', Pose), ('per_view_valid', C.c_bool),
                ('pad', C.c_bool * 3)]


class Controller(C.Structure):
    _fields_ = [('pose', Pose), ('linear_velocity', C.c_float * 3),
                ('angular_velocity', C.c_float * 3), ('hand_curl', C.c_float * 5),
                ('trigger_value', C.c_float), ('squeeze_value', C.c_float),
                ('squeeze_force', C.c_float), ('thumbstick', C.c_float * 2),
                ('trackpad_force', C.c_float), ('trackpad', C.c_float * 2)] + [
        (name, C.c_bool) for name in ('hand_tracking_active', 'active',
        'system_click', 'system_touch', 'a_click', 'a_touch', 'b_click', 'b_touch',
        'trigger_click', 'trigger_touch', 'thumbstick_click', 'thumbstick_touch',
        'trackpad_touch', 'pad0', 'pad1', 'pad2')]


class Packet(C.Structure):
    _fields_ = [('header', C.c_uint64), ('head', Head),
                ('left', Controller), ('right', Controller)]


def receive(sock):
    data = bytearray()
    while len(data) < C.sizeof(Packet):
        part = sock.recv(C.sizeof(Packet) - len(data))
        if not part:
            raise ConnectionError('Monado closed during state read')
        data.extend(part)
    if data[:8] != b'mndrmt3\0':
        raise ValueError('unexpected Monado remote protocol version')
    return Packet.from_buffer_copy(data)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--device', choices=['head', 'left', 'right'], default='right')
    p.add_argument('--position', nargs=3, type=float)
    p.add_argument('--orientation', nargs=4, type=float, help='quaternion x y z w')
    p.add_argument('--aim-at', nargs=3, type=float, help='point controller -Z at world position')
    p.add_argument('--aim-pitch-offset', type=float, default=0,
                   help='local X rotation in degrees to compensate the application ray offset')
    p.add_argument('--click', choices=['trigger', 'a', 'b'])
    p.add_argument('--hold', type=float, default=0.15)
    p.add_argument('--reset', action='store_true')
    p.add_argument('--port', type=int, default=4242)
    args = p.parse_args()
    if sys.byteorder != 'little' or C.sizeof(Packet) != 376:
        p.error('this protocol adapter requires the verified little-endian x86_64 ABI')
    if not 0 < args.hold <= 10:
        p.error('--hold must be in (0, 10] seconds')
    if args.device == 'head' and args.click:
        p.error('button clicks require a controller')
    if any(not math.isfinite(v) for values in (args.position, args.aim_at, [args.aim_pitch_offset])
           if values for v in values):
        p.error('positions and aim parameters must be finite')
    with socket.create_connection(('127.0.0.1', args.port), timeout=3) as sock:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        reset, current = receive(sock), receive(sock)
        packet = reset if args.reset else current
        target = packet.head.center if args.device == 'head' else getattr(packet, args.device).pose
        if args.position:
            target.position[:] = args.position
        if args.orientation:
            length = math.sqrt(sum(v*v for v in args.orientation))
            if not math.isfinite(length) or length < 1e-8:
                p.error('orientation must be a finite nonzero quaternion')
            target.orientation[:] = [v / length for v in args.orientation]
        if args.aim_at:
            delta = [b-a for a,b in zip(target.position, args.aim_at)]
            length = math.sqrt(sum(v*v for v in delta))
            if length < 1e-8:
                p.error('aim point must differ from controller position')
            x,y,z = [v/length for v in delta]
            q = [y, -x, 0, 1-z]
            norm = math.sqrt(sum(v*v for v in q))
            target.orientation[:] = [v/norm for v in q] if norm > 1e-8 else [0,1,0,0]
            if args.aim_pitch_offset:
                x,y,z,w = target.orientation
                h = math.radians(args.aim_pitch_offset) / 2
                s,c = math.sin(h), math.cos(h)
                target.orientation[:] = [x*c+w*s, y*c+z*s, z*c-y*s, w*c-x*s]
        # Explicit controller poses; optional hand skeleton simulation is unnecessary.
        packet.left.hand_tracking_active = packet.right.hand_tracking_active = False
        sock.sendall(bytes(packet))
        if args.click:
            device = getattr(packet, args.device)
            time.sleep(args.hold)
            setattr(device, args.click + '_click', True)
            if args.click == 'trigger':
                device.trigger_value = 1
            try:
                sock.sendall(bytes(packet))
                time.sleep(args.hold)
            finally:
                setattr(device, args.click + '_click', False)
                device.trigger_value = 0
                sock.sendall(bytes(packet))
        print(json.dumps({'device': args.device, 'position': list(target.position),
                          'orientation': list(target.orientation), 'click': args.click,
                          'packet_bytes': C.sizeof(Packet), 'sent': True}))


if __name__ == '__main__':
    main()

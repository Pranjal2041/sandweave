"""Experimental Monado remote state codec (verified little-endian x86_64 ABI)."""
import ctypes as C
import math
import sys


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


MAGIC = int.from_bytes(b'mndrmt3\0', 'little')
ACK_MAGIC = int.from_bytes(b'mndack1\0', 'little')
BUTTONS = {name for name, typ in Controller._fields_ if typ is C.c_bool and not name.startswith('pad')}
SCALARS = {'trigger_value', 'squeeze_value', 'squeeze_force', 'trackpad_force'}
VECTORS = {'position': 3, 'orientation': 4, 'linear_velocity': 3,
           'angular_velocity': 3, 'thumbstick': 2, 'trackpad': 2, 'hand_curl': 5}


def receive(sock):
    if sys.byteorder != 'little' or C.sizeof(Packet) != 376:
        raise RuntimeError('requires the verified little-endian x86_64 ABI')
    data = bytearray()
    while len(data) < C.sizeof(Packet):
        part = sock.recv(C.sizeof(Packet) - len(data))
        if not part:
            raise ConnectionError('Monado closed during state read')
        data.extend(part)
    packet = Packet.from_buffer_copy(data)
    if packet.header != MAGIC:
        raise ValueError('unexpected Monado remote protocol version')
    return packet


def update(current, changes):
    """Validate the entire update before sending anything; omitted fields persist."""
    if not isinstance(changes, dict) or set(changes) - {'head', 'left', 'right'}:
        raise ValueError('state must contain head, left and/or right')
    packet = Packet.from_buffer_copy(bytes(current))
    for name, fields in changes.items():
        if not isinstance(fields, dict):
            raise ValueError('device state must be an object')
        target = packet.head.center if name == 'head' else getattr(packet, name)
        pose = target if name == 'head' else target.pose
        allowed = {'position', 'orientation'} if name == 'head' else BUTTONS | SCALARS | VECTORS.keys()
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError('unsupported device field: ' + key)
            if key in BUTTONS:
                if type(value) is not bool:
                    raise ValueError(key + ' must be boolean')
                setattr(target, key, value)
                continue
            values = value if key in VECTORS else [value]
            if not isinstance(values, (list, tuple)) or len(values) != VECTORS.get(key, 1):
                raise ValueError('invalid vector length for ' + key)
            if any(type(v) not in (float, int) or not math.isfinite(v) or abs(v) > 1e10 for v in values):
                raise ValueError('invalid numeric value for ' + key)
            if key in SCALARS or key == 'hand_curl':
                if any(not 0 <= v <= 1 for v in values):
                    raise ValueError(key + ' must be in 0..1')
            if key in ('thumbstick', 'trackpad') and any(not -1 <= v <= 1 for v in values):
                raise ValueError(key + ' must be in -1..1')
            if key == 'orientation':
                norm = math.sqrt(sum(v*v for v in values))
                if norm < 1e-8:
                    raise ValueError('orientation must be nonzero')
                values = [v/norm for v in values]
            if key in ('position', 'orientation'):
                getattr(pose, key)[:] = values
            elif key in VECTORS:
                getattr(target, key)[:] = values
            else:
                setattr(target, key, values[0])
    return packet

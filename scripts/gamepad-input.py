#!/usr/bin/env python3
"""Write a leased gamepad state inside the guest; input returns to neutral on expiry."""
import argparse
import os
from pathlib import Path
import struct
import tempfile
import time

BUTTONS = dict(a=0, b=1, x=2, y=3, lb=4, rb=5, back=6, start=7, menu=8,
               ls=9, rs=10, left=11, right=12, up=13, down=14)
# Raw SDL numbering of an Xbox 360 pad on Linux, with aliases for the sticks (-1..1)
# and the triggers lt/rt on axes 2/5 (0..1; SDL reports a released trigger as -32768).
AXES = {**{f'axis{i}': i for i in range(8)}, 'lx': 0, 'ly': 1, 'rx': 3, 'ry': 4}
TRIGGERS = {'lt': 2, 'rt': 5}
NEUTRAL = [0, 0, -32768, 0, 0, -32768, 0, 0]
# Rumble records the SDL proxy (sdl-gamepad-proxy.c) appends: magic, output, pad, monotonic_ns, first, second, ms.
RUMBLE = struct.Struct('<4sHHQHHI')
# Per read, within the SDK wire format's 4 MiB metadata limit; 'complete' is false when more remain.
MAX_RUMBLE_EVENTS = 8192


def axis_index(name):
    if name in TRIGGERS:
        return TRIGGERS[name]
    if name in AXES:
        return AXES[name]
    raise ValueError('axes: ' + ', '.join([*AXES, *TRIGGERS]))


def axis_value(name, value):
    """Raw SDL value: triggers 0..1 span -32768..32767; sticks and raw axes -1..1 scale by 32767."""
    value = float(value)
    if name in TRIGGERS:
        if not 0 <= value <= 1:
            raise ValueError(name + ' must be in [0, 1]')
        return round(-32768 + value * 65535)
    if not -1 <= value <= 1:
        raise ValueError(name + ' must be in [-1, 1]')
    return round(value * 32767)


def read_rumble(path, after):
    """Records from index `after`; a partially visible trailing record waits for the next read."""
    if type(after) is not int or after < 0:
        raise ValueError('haptics cursor must be a non-negative record index')
    try:
        with open(path, 'rb') as source:
            source.seek(after * RUMBLE.size)
            data = source.read(MAX_RUMBLE_EVENTS * RUMBLE.size)
    except FileNotFoundError:
        data = b''
    events = []
    for offset in range(0, len(data) - len(data) % RUMBLE.size, RUMBLE.size):
        magic, output, pad, guest_ns, first, second, duration_ms = RUMBLE.unpack_from(data, offset)
        if magic != b'RUM1' or output not in (0, 1) or pad > 3:
            raise ValueError(f'corrupt haptics record {after + len(events)}')
        names = ('low_frequency', 'high_frequency') if output == 0 else ('left_trigger', 'right_trigger')
        events.append({'output': ('motors', 'triggers')[output], 'pad': pad, 'guest_ns': guest_ns,
                       names[0]: first / 0xFFFF, names[1]: second / 0xFFFF, 'duration_ms': duration_ms,
                       'stop': first == second == 0})
    return events


def write_state(path, axes, buttons, duration):
    now = time.monotonic_ns()
    data = struct.pack('<8sQQ8hIB3x', b'LABPAD1\0', now,
                       now + int(duration * 1e9), *axes, buttons, 0)
    fd, temporary = tempfile.mkstemp(prefix='.lab-gamepad-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def serve(path, haptics, lease=1.0, renew=.25):
    """Persistent state from JSON lines on stdin, renewed until changed; EOF restores neutral.

    Each line is {"buttons_down": [...], "buttons_up": [...], "axes": {name: value}}. The
    lease outlives a renewal interval, so a controller that disappears leaves the pad
    neutral within the lease. Every line is acknowledged after its state is written.
    {"haptics_after": N} instead returns the rumble the game requested from record N.
    """
    import json
    import select
    import sys
    axes, mask, sequence, pending = NEUTRAL.copy(), 0, 0, b''
    write_state(path, axes, mask, lease)
    print(json.dumps({'ready': True, 'guest_ns': time.monotonic_ns()}), flush=True)
    stdin = sys.stdin.fileno()
    try:
        while True:
            # Raw reads with our own buffer: buffered readline would hide queued lines from select.
            if b'\n' not in pending and select.select([stdin], [], [], renew)[0]:
                chunk = os.read(stdin, 65536)
                if not chunk:
                    break
                pending += chunk
            if b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                try:
                    update = json.loads(line)
                    if 'haptics_after' in update:
                        guest_ns, after = time.monotonic_ns(), update['haptics_after']
                        events = read_rumble(haptics, after)
                        # The proxy appends on request; every record written before guest_ns is included.
                        print(json.dumps({'events': events, 'first': after, 'next': after + len(events),
                                          'guest_ns': guest_ns, 'complete': len(events) < MAX_RUMBLE_EVENTS}),
                              flush=True)
                        continue
                    # Validate the whole update before applying any of it.
                    new_mask, new_axes = mask, axes.copy()
                    for name in update.get('buttons_down', []):
                        new_mask |= 1 << BUTTONS[name]
                    for name in update.get('buttons_up', []):
                        new_mask &= ~(1 << BUTTONS[name])
                    for name, value in update.get('axes', {}).items():
                        new_axes[axis_index(name)] = axis_value(name, value)
                except (KeyError, ValueError, TypeError, AttributeError) as error:
                    print(json.dumps({'error': str(error)}), flush=True)
                    continue
                mask, axes = new_mask, new_axes
                write_state(path, axes, mask, lease)
                sequence += 1
                print(json.dumps({'sequence': sequence, 'guest_ack_ns': time.monotonic_ns(),
                                  'buttons': [n for n, bit in BUTTONS.items() if mask >> bit & 1],
                                  'axes': {**{n: axes[i] / 32767 for n, i in AXES.items() if not n.startswith('axis')},
                                           **{n: (axes[i] + 32768) / 65535 for n, i in TRIGGERS.items()}}}),
                      flush=True)
            else:
                write_state(path, axes, mask, lease)
    finally:
        write_state(path, NEUTRAL, 0, 1)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state', type=Path, default=Path('/tmp/lab-gamepad.bin'))
    p.add_argument('--haptics', type=Path, default=Path('/tmp/lab-gamepad-haptics.bin'))
    p.add_argument('--button', action='append', choices=BUTTONS, default=[])
    p.add_argument('--axis', action='append', default=[], metavar='NAME=VALUE')
    p.add_argument('--hold', type=float, default=.2)
    p.add_argument('--serve', action='store_true', help='persistent state from JSON lines on stdin')
    args = p.parse_args()
    if args.serve:
        return serve(args.state, args.haptics)
    if not 0 < args.hold <= 10:
        p.error('--hold must be in (0, 10] seconds')
    axes = NEUTRAL.copy()
    for item in args.axis:
        try:
            name, value = item.split('=', 1)
            value = float(value)
        except ValueError:
            p.error('axis must have the form NAME=VALUE')
        try:
            axes[axis_index(name)] = axis_value(name, value)
        except ValueError as error:
            p.error(str(error))
    mask = sum(1 << BUTTONS[name] for name in set(args.button))
    write_state(args.state, axes, mask, args.hold + .25)
    try:
        time.sleep(args.hold)
    finally:
        write_state(args.state, NEUTRAL, 0, 1)


if __name__ == '__main__':
    main()

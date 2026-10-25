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
# Preserve raw SDL numbering, with aliases for the verified menu/aim pairs.
# Trigger calibration remains unresolved; see notes/gunspinning-vr-experiment.md.
AXES = {**{f'axis{i}': i for i in range(8)}, 'lx': 0, 'ly': 1, 'rx': 3, 'ry': 4}
NEUTRAL = [0] * 8


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


def serve(path, lease=1.0, renew=.25):
    """Persistent state from JSON lines on stdin, renewed until changed; EOF restores neutral.

    Each line is {"buttons_down": [...], "buttons_up": [...], "axes": {name: value}}. The
    lease outlives a renewal interval, so a controller that disappears leaves the pad
    neutral within the lease. Every line is acknowledged after its state is written.
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
                    # Validate the whole update before applying any of it.
                    update, new_mask, new_axes = json.loads(line), mask, axes.copy()
                    for name in update.get('buttons_down', []):
                        new_mask |= 1 << BUTTONS[name]
                    for name in update.get('buttons_up', []):
                        new_mask &= ~(1 << BUTTONS[name])
                    for name, value in update.get('axes', {}).items():
                        if name not in AXES or not -1 <= float(value) <= 1:
                            raise ValueError('axes: ' + ', '.join(AXES) + '; values must be in [-1, 1]')
                        new_axes[AXES[name]] = round(float(value) * 32767)
                except (KeyError, ValueError, TypeError) as error:
                    print(json.dumps({'error': str(error)}), flush=True)
                    continue
                mask, axes = new_mask, new_axes
                write_state(path, axes, mask, lease)
                sequence += 1
                print(json.dumps({'sequence': sequence, 'guest_ack_ns': time.monotonic_ns(),
                                  'buttons': [n for n, bit in BUTTONS.items() if mask >> bit & 1],
                                  'axes': {n: axes[i] / 32767 for n, i in AXES.items() if not n.startswith('axis')}}),
                      flush=True)
            else:
                write_state(path, axes, mask, lease)
    finally:
        write_state(path, NEUTRAL, 0, 1)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state', type=Path, default=Path('/tmp/lab-gamepad.bin'))
    p.add_argument('--button', action='append', choices=BUTTONS, default=[])
    p.add_argument('--axis', action='append', default=[], metavar='NAME=VALUE')
    p.add_argument('--hold', type=float, default=.2)
    p.add_argument('--serve', action='store_true', help='persistent state from JSON lines on stdin')
    args = p.parse_args()
    if args.serve:
        return serve(args.state)
    if not 0 < args.hold <= 10:
        p.error('--hold must be in (0, 10] seconds')
    axes = NEUTRAL.copy()
    for item in args.axis:
        try:
            name, value = item.split('=', 1)
            value = float(value)
        except ValueError:
            p.error('axis must have the form NAME=VALUE')
        if name not in AXES or not -1 <= value <= 1:
            p.error('axes: ' + ', '.join(AXES) + '; values must be in [-1, 1]')
        axes[AXES[name]] = round(value * 32767)
    mask = sum(1 << BUTTONS[name] for name in set(args.button))
    write_state(args.state, axes, mask, args.hold + .25)
    try:
        time.sleep(args.hold)
    finally:
        write_state(args.state, NEUTRAL, 0, 1)


if __name__ == '__main__':
    main()

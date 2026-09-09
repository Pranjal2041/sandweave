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


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state', type=Path, default=Path('/tmp/lab-gamepad.bin'))
    p.add_argument('--button', action='append', choices=BUTTONS, default=[])
    p.add_argument('--axis', action='append', default=[], metavar='NAME=VALUE')
    p.add_argument('--hold', type=float, default=.2)
    args = p.parse_args()
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

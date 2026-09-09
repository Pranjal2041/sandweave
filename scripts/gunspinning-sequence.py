#!/usr/bin/env python3
"""Run an explicit input/capture sequence; images still require visual verification.

Sequence JSON is a list of objects with one key: wait (seconds), capture (file
stem), gamepad (gamepad-input.py arguments), motion (vr-remote-input.py arguments),
vr_state (Monado state update), or metrics (file stem). Start from the documented
game/menu state. This is an experiment recorder, not a game completion test.
"""
import argparse
from contextlib import ExitStack
import io
import json
import math
from pathlib import Path
import re
import subprocess
import time

from environment import EnvironmentManager
from fast_io import FastIOClient


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('name')
    p.add_argument('mode', choices=('gamepad', 'motion'))
    p.add_argument('sequence', type=Path)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    m = EnvironmentManager()
    status = m.status(args.name)
    if not args.name.startswith('vr-') or status['status'] != 'running' or not status.get('gpu'):
        p.error('select a running disposable GPU guest named vr-*')
    sequence = json.loads(args.sequence.read_text())
    if not isinstance(sequence, list):
        p.error('sequence must be a list')
    for step in sequence:
        if not isinstance(step, dict) or len(step) != 1:
            p.error('each step must have one key')
        key, value = next(iter(step.items()))
        if key in ('capture', 'metrics'):
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
                p.error('artifact names must be simple file stems')
        elif key == 'wait':
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 60:
                p.error('wait must be in 0..60 seconds')
        elif key in ('gamepad', 'motion'):
            if key != args.mode or not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                p.error('input arguments must be strings and match the selected mode')
        elif key != 'vr_state' or args.mode != 'motion':
            p.error('unsupported step: ' + key)
    args.output.mkdir(parents=True, exist_ok=False)
    prefix = [*m._command(args.name), 'exec', args.name]

    def guest(*command):
        # Parent pipe avoids this runtime's regular-file output-offset issue.
        return subprocess.check_output([*prefix, *command], timeout=30)

    records = []
    try:
        with ExitStack() as stack:
            client = stack.enter_context(FastIOClient(args.name, manager=m)) if args.mode == 'gamepad' else None
            for step in sequence:
                key, value = next(iter(step.items()))
                record = {'step': step, 'started_unix_ns': time.time_ns()}
                records.append(record)
                if key == 'wait':
                    time.sleep(value)
                elif key in ('gamepad', 'motion'):
                    command = (['runuser', '-u', 'ga', '--', 'python3', '/opt/gunspinning-lab/gamepad-input.py']
                               if key == 'gamepad' else ['python3', '/opt/vr/vr-remote-input.py'])
                    guest(*command, *value)
                elif key == 'vr_state':
                    guest('python3', '-c', '''
import json, socket, sys, time
sys.path.insert(0, '/opt/vr')
from vr_input import receive, update
with socket.create_connection(('127.0.0.1', 4242), timeout=3) as s:
    receive(s)
    packet = update(receive(s), json.loads(sys.argv[1]))
    s.sendall(bytes(packet))
    time.sleep(.1)
''', json.dumps(value))
                elif key == 'metrics':
                    (args.output / (value + '.pb')).write_bytes(guest('cat', '/tmp/monado-metrics.pb'))
                elif client:
                    client.screenshot(fresh=True).save(args.output / (value + '.png'))
                    record['frame'] = client.last_metadata
                else:
                    guest('runuser', '-u', 'ga', '--', 'touch', '/tmp/monado-eye.ppm.request')
                    deadline = time.monotonic() + 10
                    while guest('sh', '-c', 'test -e /tmp/monado-eye.ppm.request && echo pending || true').strip():
                        if time.monotonic() > deadline:
                            raise TimeoutError('no composed eye image')
                        time.sleep(.02)
                    from PIL import Image
                    Image.open(io.BytesIO(guest('cat', '/tmp/monado-eye.ppm'))).save(args.output / (value + '.png'))
                record['completed_unix_ns'] = time.time_ns()
                print(key, value if key in ('capture', 'metrics') else '', flush=True)
    finally:
        (args.output / 'sequence.json').write_text(json.dumps({
            'name': args.name, 'mode': args.mode, 'steps': records,
            'scope': 'Input sent and artifacts captured; inspect images to establish game acceptance.',
        }, indent=2) + '\n')


if __name__ == '__main__':
    main()

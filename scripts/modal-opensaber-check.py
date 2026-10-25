#!/usr/bin/env python3
"""Run Open Saber with runtime="modal", retaining both eye videos, inputs and Monado timings."""
import argparse
import importlib.util
import json
import math
from pathlib import Path
import sys
import time
import traceback

from PIL import Image

from sandweave import Sandbox


def trajectory(t):
    state = {'head': {'position': [.04 * math.sin(t * .4), 1.6, 0]}}
    for side, sign in [('left', -1), ('right', 1)]:
        phase = t * 3 + (0 if sign == 1 else math.pi)
        angle = .65 * math.sin(phase)
        state[side] = {
            'position': [sign * .25 + .18 * math.sin(phase), 1.3 + .22 * math.cos(phase), -.5],
            'orientation': [math.sin(angle / 2), 0, 0, math.cos(angle / 2)],
            'linear_velocity': [.54 * math.cos(phase), -.66 * math.sin(phase), 0],
            'angular_velocity': [1.95 * math.cos(phase), 0, 0],
        }
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=120)
    parser.add_argument('--gpu', default='L40S')
    parser.add_argument('--startup-timeout', type=float, default=3600)
    args = parser.parse_args()
    if not 0 < args.seconds <= 300:
        parser.error('seconds must be in 0..300')
    args.output.mkdir(parents=True, exist_ok=False)
    from sandweave import GPU
    try:
        started = time.monotonic()
        with Sandbox(template='vr/opensaber', runtime='modal', gpu=GPU(model=args.gpu),
                     startup_timeout=args.startup_timeout) as sandbox:
            ready = {'event': 'ready', 'seconds': time.monotonic() - started, 'id': sandbox.id,
                     'timings': sandbox._info.get('timings')}
            (args.output / 'ready.json').write_text(json.dumps(ready, indent=2) + '\n')
            print(json.dumps(ready), flush=True)
            observation = sandbox.vr.observe(timeout=60)
            Image.fromarray(observation.left).save(args.output / 'menu-left.png')
            Image.fromarray(observation.right).save(args.output / 'menu-right.png')
            sandbox.vr.action({'right': {'position': [.2, 1.3, -.5],
                                        'orientation': [-.4815723002, .0920430496, .0381254815, .8707253337]}})
            time.sleep(.2)
            sandbox.vr.action({'right': {'trigger_click': True, 'trigger_value': 1}})
            time.sleep(.2)
            sandbox.vr.action({'right': {'trigger_click': False, 'trigger_value': 0}})
            time.sleep(3)
            recording = sandbox.vr.record(args.output / 'recording', fps=30)
            with recording, (args.output / 'inputs.jsonl').open('w') as inputs:
                begin = time.monotonic()
                while time.monotonic() - begin < args.seconds:
                    tick = time.monotonic()
                    action = trajectory(tick - begin)
                    response = sandbox.vr.action(action)
                    inputs.write(json.dumps({'elapsed_seconds': tick - begin,
                                             'action': action, 'ack': response}) + '\n')
                    time.sleep(max(0, 1 / 120 - (time.monotonic() - tick)))
            sandbox.files.download('/tmp/monado-metrics.pb', args.output / 'monado-metrics.pb')
            sandbox.files.download('/opt/vr/monado_metrics_pb2.py', args.output / 'monado_metrics_pb2.py')
            sys.path.insert(0, str(args.output.resolve()))
            spec = importlib.util.spec_from_file_location('vr_metrics', Path(__file__).with_name('vr-metrics.py'))
            metrics = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(metrics)
            window = {'start_ns': recording.timeline[0]['capture_begin_ns'],
                      'end_ns': recording.timeline[-1]['capture_begin_ns']}
            result = {'recording': recording.metadata, 'capture_window': window,
                      'monado': metrics.summarize(args.output / 'monado-metrics.pb', args.seconds, **window)}
            (args.output / 'summary.json').write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result, indent=2), flush=True)
    except BaseException as error:
        (args.output / 'failure.json').write_text(json.dumps({
            'error': str(error), 'type': type(error).__name__,
            'sandbox_id': getattr(error, 'sandbox_id', None), 'phase': getattr(error, 'phase', None),
            'traceback': traceback.format_exc()}, indent=2) + '\n')
        raise


if __name__ == '__main__':
    main()

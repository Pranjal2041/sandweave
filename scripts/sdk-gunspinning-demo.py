#!/usr/bin/env python3
"""Exercise actual gameplay through template controls and retain honest visual evidence."""
import argparse
import importlib.util
import json
import math
from pathlib import Path
import statistics
import time

from sandweave import Sandbox, Slurm


def aiming(position, target, pitch=50):
    delta = [b-a for a, b in zip(position, target)]
    length = math.sqrt(sum(v*v for v in delta))
    x, y, z = [v/length for v in delta]
    q = [y, -x, 0, 1-z]
    norm = math.sqrt(sum(v*v for v in q))
    x, y, z, w = [v/norm for v in q]
    s, c = math.sin(math.radians(pitch)/2), math.cos(math.radians(pitch)/2)
    return {'position': position, 'orientation': [x*c+w*s, y*c+z*s, z*c-y*s, w*c-x*s]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', required=True)
    parser.add_argument('--mode', choices=('motion', 'gamepad'), default='motion')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    target = Slurm.connect(args.job, cpus=12)
    recipe = 'vr/gunspinning' if args.mode == 'motion' else 'games/gunspinning-gamepad'
    actions = []
    report = {'mode': args.mode, 'actions': actions, 'complete': False}
    try:
        with Sandbox(template=recipe, gpu='L40S', target=target) as env:
            report.update(id=env.id, timings=env.timings)
            if args.mode == 'gamepad':
                env.desktop.keyboard.press(['ESC'])
                time.sleep(15)
                env.run('xdotool search --name GunSpinning windowactivate --sync', user='ga')
                sequence = json.loads(Path('notes/gunspinning-gamepad-sequence.json').read_text())
                for step in sequence:
                    if 'wait' in step:
                        time.sleep(step['wait'])
                    elif 'capture' in step:
                        env.desktop.screenshot().save(args.output / (step['capture'] + '.png'))
                    elif 'gamepad' in step:
                        values = iter(step['gamepad']); buttons, axes, hold = [], {}, .2
                        for key in values:
                            value = next(values)
                            if key == '--button': buttons.append(value)
                            elif key == '--axis':
                                name, value = value.split('='); axes[name] = float(value)
                            elif key == '--hold': hold = float(value)
                        actions.append(env.gamepad.action(buttons=buttons, axes=axes, hold=hold))
                report['presentation'] = 'flat desktop gamepad mode; no synthetic stereo'
                (args.output / 'game.log').write_text(env.files.read_text('/tmp/gunspinning-gamepad.log'))
            else:
                time.sleep(8)
                transport = {}
                for codec in ('raw', 'zstd'):
                    env.vr.config['transport'] = codec
                    samples, sizes, encodes = [], [], []
                    for _ in range(20):
                        started = time.perf_counter(); observation = env.vr.observe()
                        samples.append(time.perf_counter()-started)
                        sizes.append(observation.metadata['transport']['bytes'])
                        encodes.append(observation.metadata['transport']['encode_ns'])
                    transport[codec] = {'median_ms': statistics.median(samples)*1000,
                                        'median_bytes': statistics.median(sizes), 'median_encode_ns': statistics.median(encodes)}
                report['transport'] = transport
                def send(label, state):
                    actions.append({'label': label, 'state': state, 'ack': env.vr.action(state)})
                def trigger(label):
                    send(label, {'right': {'trigger_click': True, 'trigger_value': 1}})
                    time.sleep(.2)
                    send(label+' release', {'right': {'trigger_click': False, 'trigger_value': 0}})
                    time.sleep(.4)
                send('Initial pose', {'head': {'position': [0, 1.6, 0], 'orientation': [0, 0, 0, 1]},
                                      'left': aiming([-.2, 1.4, -.2], [-.2, 1.2, -3]),
                                      'right': aiming([.2, 1.4, -.2], [0, 1.2, -3])})
                with env.vr.record(args.output / 'frames', fps=30) as recording:
                    time.sleep(2); trigger('Play')
                    send('Aim at Training', {'right': aiming([.2, 1.4, -.2], [.2, 1.2, -3])})
                    time.sleep(.4); trigger('Training'); time.sleep(13)
                    send('Aim at bottles', {'right': aiming([.2, 1.4, -.2], [-.4, 1.2, -3])})
                    time.sleep(2)
                    for i in range(3): trigger('Shot ' + str(i+1))
                    time.sleep(2)
                    send('Reload', {'right': {'thumbstick': [0, -1], 'thumbstick_click': True}})
                    time.sleep(.3)
                    send('Release reload', {'right': {'thumbstick': [0, 0], 'thumbstick_click': False}})
                    time.sleep(2)
                    started = time.monotonic()
                    while (elapsed := time.monotonic()-started) < 6:
                        phase = elapsed/6*2*math.pi; yaw = .12*math.sin(phase)
                        send('Tracking sweep', {'head': {'position': [.08*math.sin(phase), 1.6, 0],
                             'orientation': [0, math.sin(yaw/2), 0, math.cos(yaw/2)]},
                             'right': aiming([.2, 1.4, -.2], [.6*math.sin(phase), 1.2, -3])})
                        time.sleep(1/30)
                    time.sleep(2)
                report['recording'] = recording.metadata
                env.files.download('/tmp/monado-metrics.pb', args.output / 'monado-metrics.pb')
                spec = importlib.util.spec_from_file_location('vr_metrics', Path(__file__).with_name('vr-metrics.py'))
                metrics = importlib.util.module_from_spec(spec); spec.loader.exec_module(metrics)
                report['application_after_export'] = metrics.summarize(args.output / 'monado-metrics.pb', 10)
                spec = importlib.util.spec_from_file_location('recording_metrics', Path(__file__).with_name('sdk-recording-metrics.py'))
                aligned = importlib.util.module_from_spec(spec); spec.loader.exec_module(aligned)
                report['application_during_recording'] = aligned.summarize_recording(
                    args.output / 'frames', args.output / 'monado-metrics.pb')
            report['complete'] = True
            print(json.dumps({k: v for k, v in report.items() if k != 'actions'}, indent=2), flush=True)
    except BaseException as error:
        report['error'] = str(error)
        raise
    finally:
        (args.output / 'result.json').write_text(json.dumps(report, indent=2))
        connection = target.connection()
        try:
            connection.call('_shutdown_if_idle')
        finally:
            connection.close()


if __name__ == '__main__':
    main()

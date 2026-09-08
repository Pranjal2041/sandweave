#!/usr/bin/env python3
"""Measure continuous VR observations and inputs; optionally record lossless images and video.

The demo drives virtual controllers on a timed trajectory. It is not a model or
physical tracking test. Monado/Open Saber must be stopped before this command.
"""
import argparse
import importlib.util
import json
import math
from pathlib import Path
import subprocess
import threading
import time

from vr_stream import VRStream, FrameRecorder


def distribution(values):
    values = sorted(values)
    return {key: values[min(len(values)-1, int((len(values)-1)*fraction))]
            for key, fraction in [('p50', .5), ('p95', .95), ('p99', .99), ('max', 1)]} if values else None


def trajectory(t):
    state = {'head': {'position': [.04*math.sin(t*.4), 1.6, 0]}}
    for side, sign in [('left', -1), ('right', 1)]:
        phase = t*3 + (0 if sign == 1 else math.pi)
        angle = .65*math.sin(phase)
        state[side] = {'position': [sign*.25+.18*math.sin(phase), 1.3+.22*math.cos(phase), -.5],
                       'orientation': [math.sin(angle/2), 0, 0, math.cos(angle/2)],
                       'linear_velocity': [.54*math.cos(phase), -.66*math.sin(phase), 0],
                       'angular_velocity': [1.95*math.cos(phase), 0, 0]}
    return state


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('name')
    p.add_argument('--seconds', type=float, default=20)
    p.add_argument('--warmup', type=float, default=20)
    p.add_argument('--fps', type=float, default=90, help='maximum observation rate')
    p.add_argument('--input-hz', type=float, default=120)
    p.add_argument('--hz', type=int, default=120)
    p.add_argument('--width', type=int, default=960, help='width per eye; both eyes are captured')
    p.add_argument('--height', type=int, default=1080, help='height per eye')
    p.add_argument('--mirror', choices=['none', 'pbo', 'sync'], default='none')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--record', action='store_true')
    p.add_argument('--video', action='store_true')
    args = p.parse_args()
    if not (0 < args.seconds <= 300 and 0 <= args.warmup <= 120 and 1 <= args.input_hz <= 500):
        p.error('invalid duration, warmup or input frequency')
    if args.video and not args.record:
        p.error('--video requires --record')
    args.output.mkdir(parents=True, exist_ok=False)
    recorder = None
    try:
        with VRStream(args.name, width=args.width, height=args.height, fps=args.fps,
                      hz=args.hz, mirror=args.mirror) as vr:
            print('VR stream ready; warming up the game', flush=True)
            time.sleep(args.warmup)
            vr.latest().image().save(args.output/'menu.png')
            # Open Saber 0.5.0 initial Play button: same verified menu pose as vr-lab.py.
            vr.input({'right': {'position': [.2, 1.3, -.5],
                               'orientation': [-.4815723002, .0920430496, .0381254815, .8707253337]}})
            time.sleep(.2)
            vr.input({'right': {'trigger_click': True, 'trigger_value': 1}})
            time.sleep(.2)
            vr.input({'right': {'trigger_click': False, 'trigger_value': 0}})
            time.sleep(3)
            vr.latest().image().save(args.output/'gameplay-start.png')
            if args.record:
                recorder = FrameRecorder(args.output/'frames')
            frames, actions, errors = [], [], []
            stop = threading.Event()
            start = time.monotonic()
            def inputs():
                scheduled = start
                try:
                    with (args.output/'inputs.jsonl').open('w') as log:
                        while not stop.is_set():
                            now = time.monotonic()
                            if now < scheduled:
                                stop.wait(scheduled-now)
                                continue
                            state = trajectory(now-start)
                            ack = vr.input(state)
                            actions.append(ack)
                            log.write(json.dumps({**ack, 'state': state})+'\n')
                            scheduled += 1/args.input_hz
                            if scheduled < time.monotonic()-1/args.input_hz:
                                scheduled = time.monotonic()+1/args.input_hz
                except BaseException as error:
                    errors.append(error)
                    stop.set()
            thread = threading.Thread(target=inputs)
            thread.start()
            last = vr.latest().sequence
            initial = last
            try:
                while time.monotonic()-start < args.seconds and not stop.is_set():
                    frame = vr.latest(last)
                    frames.append(frame.metadata())
                    last = frame.sequence
                    if recorder:
                        recorder.submit(frame)
            finally:
                stop.set()
                thread.join(timeout=10)
            if thread.is_alive():
                raise TimeoutError('input worker failed to stop')
            if errors:
                raise errors[0]
            frame.image().save(args.output/'gameplay-end.png')
            frame.image('left').save(args.output/'left-eye.png')
            frame.image('right').save(args.output/'right-eye.png')
            elapsed = time.monotonic()-start
            metric_file = args.output/'monado-metrics.pb'
            raw = subprocess.run([*vr.manager._command(args.name), 'exec', args.name,
                                  'cat', '/tmp/monado-metrics.pb'], capture_output=True, check=True).stdout
            metric_file.write_bytes(raw)
            spec = importlib.util.spec_from_file_location('vr_metrics', Path(__file__).with_name('vr-metrics.py'))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            metrics = module.summarize(metric_file, args.seconds)
            clock = {'guest_to_host_ns': vr.guest_to_host_ns, 'minimum_ping_rtt_ns': vr.clock_rtt_ns,
                     'offset_uncertainty_ns': vr.clock_rtt_ns/2}
            (args.output/'observations.jsonl').write_text(''.join(json.dumps(f)+'\n' for f in frames))
            result = {'options': {k: str(v) if isinstance(v, Path) else v for k,v in vars(args).items()},
                      'seconds': elapsed, 'observations': len(frames),
                      'observation_unit': 'stereo pair', 'eye_count': 2,
                      'eye_width': args.width, 'eye_height': args.height,
                      'packed_width': args.width*2, 'layout': 'left-right side-by-side',
                      'observation_fps': (len(frames)-1)*1e9/(frames[-1]['host_observed_ns']-frames[0]['host_observed_ns']),
                      'ring_frames_skipped': last-initial-len(frames),
                      'input_updates': len(actions), 'input_hz': (len(actions)-1)*1e9/(actions[-1]['host_sent_ns']-actions[0]['host_sent_ns']),
                      'clock': clock, 'monado': metrics, 'latency_ms': {}}
            result['latency_ms']['gpu_readback'] = distribution([(f['gpu_ready_ns']-f['capture_begin_ns'])/1e6 for f in frames])
            result['latency_ms']['ring_publish_copy'] = distribution([(f['published_ns']-f['gpu_ready_ns'])/1e6 for f in frames])
            result['latency_ms']['host_owned_copy'] = distribution([f['host_copy_ns']/1e6 for f in frames])
            result['latency_ms']['publish_to_host_observed'] = distribution([(f['host_observed_ns']-vr.guest_to_host_ns-f['published_ns'])/1e6 for f in frames])
            result['latency_ms']['capture_begin_to_host_observed'] = distribution([(f['host_observed_ns']-vr.guest_to_host_ns-f['capture_begin_ns'])/1e6 for f in frames])
            result['latency_ms']['input_ack_round_trip'] = distribution([a['round_trip_ns']/1e6 for a in actions])
            result['latency_ms']['observation_interval'] = distribution([(b['host_observed_ns']-a['host_observed_ns'])/1e6 for a,b in zip(frames,frames[1:])])
        if recorder:
            recorder.close()
            result['recording'] = {'saved': recorder.saved, 'dropped': recorder.dropped,
                                   'queue_capacity': recorder.queue.maxsize}
            if args.video:
                result['video'] = str(recorder.video())
            recorder = None
        (args.output/'summary.json').write_text(json.dumps(result, indent=2)+'\n')
        print(json.dumps(result, indent=2), flush=True)
    finally:
        if recorder:
            recorder.close()


if __name__ == '__main__':
    main()

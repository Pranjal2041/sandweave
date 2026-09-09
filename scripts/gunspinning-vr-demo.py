#!/usr/bin/env python3
"""Record both GunSpinning VR eyes with the existing continuous Monado stream.

Prepare the disposable GPU guest, then stop its game and Monado before running.
This owns the temporary Monado stream and GunSpinning process; Xvnc stays alive.
Exports synchronized stereo/left/right videos after lossless recording closes.
"""
import argparse
import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys
import threading
import time

from vr_stream import VRStream, FrameRecorder


def aiming(position, target, pitch=50):
    delta = [b-a for a, b in zip(position, target)]
    length = math.sqrt(sum(v*v for v in delta))
    x, y, z = [v/length for v in delta]
    q = [y, -x, 0, 1-z]
    norm = math.sqrt(sum(v*v for v in q))
    x, y, z, w = [v/norm for v in q]
    s, c = math.sin(math.radians(pitch)/2), math.cos(math.radians(pitch)/2)
    return {'position': position,
            'orientation': [x*c+w*s, y*c+z*s, z*c-y*s, w*c-x*s]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('name')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--fps', type=float, default=30, help='maximum stereo capture cadence')
    p.add_argument('--warmup', type=float, default=20)
    p.add_argument('--video', action=argparse.BooleanOptionalAction, default=True,
                   help='export all three viewing videos after recording')
    args = p.parse_args()
    if not 1 <= args.fps <= 90 or not 0 <= args.warmup <= 120:
        p.error('fps must be 1..90 and warmup 0..120 seconds')
    # Validate the generated metrics module before starting any guest processes.
    spec = importlib.util.spec_from_file_location('metrics', Path(__file__).with_name('vr-metrics.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args.output.mkdir(parents=True, exist_ok=False)
    recorder = None
    actions, observations, errors = [], [], []
    stop = threading.Event()
    worker = None
    try:
        with VRStream(args.name, fps=args.fps, hz=90, start_game=False) as vr:
            probe = [sys.executable, str(Path(__file__).with_name('gunspinning-probe.py')),
                     args.name]
            subprocess.run([*probe, 'start', '--mode', 'motion'], check=True)
            try:
                vr.latest(timeout=60)
                time.sleep(args.warmup)
                vr.input({'head': {'position': [0, 1.6, 0], 'orientation': [0, 0, 0, 1]},
                          'left': aiming([-.2, 1.4, -.2], [-.2, 1.2, -3]),
                          'right': aiming([.2, 1.4, -.2], [0, 1.2, -3])})
                recorder = FrameRecorder(args.output/'frames')
                started = time.monotonic_ns()

                def capture():
                    last = 0
                    try:
                        while not stop.is_set():
                            frame = vr.latest(last, timeout=10)
                            last = frame.sequence
                            observations.append(frame.metadata())
                            recorder.submit(frame)
                    except BaseException as error:
                        errors.append(error)
                        stop.set()

                def send(label, state):
                    if stop.is_set():
                        raise RuntimeError('capture stopped unexpectedly')
                    ack = vr.input(state)
                    actions.append({'label': label, 'state': state, **ack,
                                    'elapsed_seconds': (ack['host_sent_ns']-started)/1e9})

                def wait(seconds):
                    if stop.wait(seconds):
                        raise RuntimeError('capture stopped unexpectedly')

                def trigger(label):
                    send(label, {'right': {'trigger_click': True, 'trigger_value': 1}})
                    wait(.2)
                    send(label+' release', {'right': {'trigger_click': False, 'trigger_value': 0}})
                    wait(.4)

                worker = threading.Thread(target=capture)
                worker.start()
                wait(2)
                trigger('Play')
                send('Aim at Training', {'right': aiming([.2, 1.4, -.2], [.2, 1.2, -3])})
                wait(.4)
                trigger('Training')
                wait(13)
                send('Aim at bottles', {'right': aiming([.2, 1.4, -.2], [-.4, 1.2, -3])})
                wait(2)
                for i in range(3):
                    trigger('Shot '+str(i+1))
                wait(2)
                send('Reload', {'right': {'thumbstick': [0, -1], 'thumbstick_click': True}})
                wait(.3)
                send('Reload release', {'right': {'thumbstick': [0, 0], 'thumbstick_click': False}})
                wait(2)
                # Smooth, modest tracking-space motions make both eye views and
                # the two hands visibly change without leaving the bottle range.
                sweep_start = time.monotonic()
                while (elapsed := time.monotonic()-sweep_start) < 6:
                    phase = elapsed/6*2*math.pi
                    yaw = .12*math.sin(phase)
                    send('Head and controller sweep', {
                        'head': {'position': [.08*math.sin(phase), 1.6+.04*math.sin(phase/2), 0],
                                 'orientation': [0, math.sin(yaw/2), 0, math.cos(yaw/2)]},
                        'right': aiming([.2, 1.4+.06*math.sin(phase), -.2],
                                        [.6*math.sin(phase), 1.2, -3]),
                        'left': aiming([-.2, 1.4-.06*math.sin(phase), -.2],
                                       [-.6*math.sin(phase), 1.2, -3])})
                    wait(1/30)
                wait(2)
                vr.latest().image().save(args.output/'final-stereo.png')
                raw = subprocess.check_output([*vr.manager._command(args.name), 'exec', args.name,
                                                'cat', '/tmp/monado-metrics.pb'], timeout=30)
                (args.output/'monado-metrics.pb').write_bytes(raw)
                clock = {'guest_to_host_ns': vr.guest_to_host_ns,
                         'minimum_ping_rtt_ns': vr.clock_rtt_ns}
            finally:
                stop.set()
                if worker:
                    worker.join(timeout=12)
                subprocess.run([*probe, 'stop', '--mode', 'motion'], check=True)
            if worker and worker.is_alive():
                raise TimeoutError('capture thread did not stop')
            if errors:
                raise errors[0]
        recorder.close()
        duration = (observations[-1]['capture_begin_ns']-observations[0]['capture_begin_ns'])/1e9
        result = {'guest': args.name, 'eye_count': 2, 'eye_size': [960, 1080],
                  'layout': 'left-right side-by-side', 'capture_limit_fps': args.fps,
                  'capture_duration_seconds': duration, 'frames_received': len(observations),
                  'observation_fps': (len(observations)-1)/duration,
                  'frames_saved': recorder.saved, 'recorder_dropped': recorder.dropped,
                  'ring_frames_skipped': observations[-1]['sequence']-observations[0]['sequence']+1-len(observations),
                  'clock': clock, 'inputs_acknowledged': len(actions),
                  'scope': 'Actual compositor eye images and acknowledged virtual input; visual video verification required. Capture cadence is not application FPS.'}
        result['monado'] = module.summarize(args.output/'monado-metrics.pb', 10)
        (args.output/'inputs.jsonl').write_text(''.join(json.dumps(a)+'\n' for a in actions))
        (args.output/'observations.jsonl').write_text(''.join(json.dumps(o)+'\n' for o in observations))
        (args.output/'summary.json').write_text(json.dumps(result, indent=2)+'\n')
        print(json.dumps(result, indent=2), flush=True)
        if args.video:
            result['videos'] = {label: str(recorder.video(eye))
                                for label, eye in [('stereo', None), ('left', 'left'), ('right', 'right')]}
            (args.output/'summary.json').write_text(json.dumps(result, indent=2)+'\n')
        recorder = None
    finally:
        (args.output/'inputs.jsonl').write_text(''.join(json.dumps(a)+'\n' for a in actions))
        (args.output/'observations.jsonl').write_text(''.join(json.dumps(o)+'\n' for o in observations))
        if recorder:
            recorder.close()


if __name__ == '__main__':
    main()

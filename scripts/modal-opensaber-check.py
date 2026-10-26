#!/usr/bin/env python3
"""Run Open Saber with runtime="modal", retaining both eye videos, inputs, audio, haptics and Monado timings.

During the recording, the game's audio and the controllers' haptic commands are read
every half second (each read continues from the previous one). Afterwards the script
exports the audio as WAV, muxes it into the side-by-side video on the shared guest
clock, saves the recorded frames nearest the first haptic pulses, and runs unified
steps (sandweave.steps) that observe stereo, audio and haptics.
"""
import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback
import wave

import numpy as np
from PIL import Image

from sandweave import Sandbox
from sandweave.games.state import GameState
from sandweave.steps import run as run_step


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


class Streams:
    """Read audio and haptics continuously; each read returns what followed the previous one."""

    def __init__(self, sandbox, directory):
        self.sandbox, self.directory = sandbox, directory
        self.chunks, self.events, self.errors = [], [], []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def poll(self):
        audio = self.sandbox.audio.observe()
        self.chunks.append(audio)
        haptics = self.sandbox.vr.haptics()
        self.events += haptics['events']
        return audio, haptics

    def _loop(self):
        try:
            while not self.stop.wait(.5):
                self.poll()
        except BaseException as error:
            self.errors.append(error)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=30)
        if self.errors:
            raise self.errors[0]
        self.poll()


def compact(observed):
    state = observed['state']
    return {'guest_ns': observed['guest_ns'], 'song_t': state['song_t'], 'score': state['score'],
            'combo': state['combo'], 'active': state['active'], 'counts': state['counts'],
            'map': (state['map'] or {}).get('difficulty'), 'results': len(state['results']), 'records': state['records']}


def dbfs(audio):
    pcm = np.frombuffer(audio['pcm'], dtype='<i2').astype(np.float64) / 32768
    return round(float(20 * np.log10(np.sqrt((pcm ** 2).mean()) + 1e-12)), 1) if len(pcm) else None


def audio_summary(chunks, path):
    """Write the tiled chunks as WAV; check continuity and loudness. Returns (summary, anchor_ns)."""
    gaps = [b['first_frame'] - a['next'] for a, b in zip(chunks, chunks[1:]) if b['first_frame'] != a['next']]
    rate, channels = chunks[0]['rate'], chunks[0]['channels']
    pcm = b''.join(chunk['pcm'] for chunk in chunks)
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(pcm)
    samples = np.frombuffer(pcm, dtype='<i2').reshape(-1, channels).astype(np.float64) / 32768
    seconds = samples[:len(samples) // rate * rate].reshape(-1, rate, channels)
    rms_db = 20 * np.log10(np.sqrt((seconds ** 2).mean(axis=(1, 2))) + 1e-12)
    # Guest time of the first frame: the lowest receipt delay over all reads bounds capture latency.
    first = chunks[0]['first_frame']
    anchor = min(chunk['end_ns'] - (chunk['next'] - first) * 10**9 // rate for chunk in chunks if chunk['frames'])
    return {'frames': len(samples), 'seconds': len(samples) / rate, 'rate': rate, 'channels': channels,
            'reads': len(chunks), 'gaps': gaps, 'dropped_frames': sum(c['dropped_frames'] for c in chunks),
            'first_frame_guest_ns': anchor, 'peak': float(np.abs(samples).max()) if len(samples) else 0,
            'rms_dbfs_per_second': [round(float(v), 1) for v in rms_db],
            'seconds_above_minus_60_dbfs': int((rms_db > -60).sum())}, anchor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=120)
    parser.add_argument('--gpu', default='L40S', help='GPU model, or comma-separated models in preference order')
    parser.add_argument('--startup-timeout', type=float, default=3600)
    args = parser.parse_args()
    if not 0 < args.seconds <= 300:
        parser.error('seconds must be in 0..300')
    args.output.mkdir(parents=True, exist_ok=False)
    from sandweave import GPU
    try:
        started = time.monotonic()
        models = tuple(args.gpu.split(','))
        with Sandbox(template='vr/opensaber', runtime='modal', gpu=GPU(model=models[0] if len(models) == 1 else models),
                     startup_timeout=args.startup_timeout) as sandbox:
            placement = sandbox.run("tr '\\0' '\\n' < /proc/1/environ | grep -E '^MODAL_(REGION|CLOUD_PROVIDER)='",
                                    user='root').stdout.split()
            ready = {'event': 'ready', 'seconds': time.monotonic() - started, 'id': sandbox.id,
                     'timings': sandbox._info.get('timings'),
                     'gpu': sandbox.run('nvidia-smi --query-gpu=name,driver_version --format=csv,noheader').stdout.strip(),
                     'placement': dict(line.split('=', 1) for line in placement),
                     'worker_region': os.environ.get('MODAL_REGION'),
                     'worker_cloud': os.environ.get('MODAL_CLOUD_PROVIDER'),
                     'requested_region': os.environ.get('SANDWEAVE_MODAL_REGION'),
                     'requested_cloud': os.environ.get('SANDWEAVE_MODAL_CLOUD')}
            observation = sandbox.vr.observe(timeout=60)
            ready['worker_to_guest_minimum_rtt_ms'] = observation.metadata['minimum_ping_rtt_ns'] / 1e6
            (args.output / 'ready.json').write_text(json.dumps(ready, indent=2) + '\n')
            print(json.dumps(ready), flush=True)
            Image.fromarray(observation.left).save(args.output / 'first-left.png')
            Image.fromarray(observation.right).save(args.output / 'first-right.png')
            # Click Play until the song is heard: the menu is silent, and the first frames may be the splash.
            startup, song, clicks = Streams(sandbox, args.output), None, 0
            deadline = time.monotonic() + 120
            while song is None:
                if time.monotonic() > deadline:
                    raise RuntimeError('the song did not start: no audio above -40 dBFS after clicking Play')
                menu, clicks = sandbox.vr.observe(), clicks + 1
                sandbox.vr.action({'right': {'position': [.2, 1.3, -.5],
                                            'orientation': [-.4815723002, .0920430496, .0381254815, .8707253337]}})
                time.sleep(.2)
                sandbox.vr.action({'right': {'trigger_click': True, 'trigger_value': 1}})
                time.sleep(.2)
                sandbox.vr.action({'right': {'trigger_click': False, 'trigger_value': 0}})
                for _ in range(8):
                    time.sleep(.5)
                    audio, _ = startup.poll()
                    pcm = np.frombuffer(audio['pcm'], dtype='<i2').astype(np.float64) / 32768
                    if len(pcm) and 20 * np.log10(np.sqrt((pcm ** 2).mean()) + 1e-12) > -40:
                        song = {'heard_at_guest_ns': audio['end_ns'], 'clicks': clicks}
                        break
            Image.fromarray(menu.left).save(args.output / 'menu-left.png')
            Image.fromarray(menu.right).save(args.output / 'menu-right.png')
            time.sleep(2)
            # Everything the game played and commanded since capture began (startup, menu, song start).
            startup.poll()
            startup_audio, _ = audio_summary(startup.chunks, args.output / 'startup-audio.wav')
            startup_audio['song'] = song
            # Game-time control and live game state, while the song plays.
            game_state = GameState('open-saber', source='/run/user/1000/open-saber')
            timing = {'state_before': compact(game_state.observe(sandbox))}
            pause = sandbox.vr.time(scale=0)
            # Every audio read joins the startup chain, so the gameplay audio still continues it.
            startup.chunks.append(sandbox.audio.observe())
            time.sleep(2)
            paused_audio = sandbox.audio.observe()
            startup.chunks.append(paused_audio)
            audio_summary([paused_audio], args.output / 'paused-audio.wav')
            pcm = np.frombuffer(paused_audio['pcm'], dtype='<i2').astype(np.float64) / 32768
            tenths = pcm[:len(pcm) // 9600 * 9600].reshape(-1, 9600)
            paused_profile = [round(float(20 * np.log10(np.sqrt((t ** 2).mean()) + 1e-12)), 1) for t in tenths]
            still = sandbox.vr.time(scale=0)
            advanced = sandbox.vr.time(advance_ticks=90)
            slow = sandbox.vr.time(scale=.5)
            time.sleep(4)
            resumed = sandbox.vr.time(scale=1)
            time.sleep(1)
            playing_audio = sandbox.audio.observe()
            startup.chunks.append(playing_audio)
            timing.update(
                pause=pause, still=still, advanced=advanced, slow=slow, resumed=resumed,
                paused_song_seconds=still['song_position'] - pause['song_position'],
                advance_ticks=advanced['ticks'] - still['ticks'],
                advance_song_seconds=advanced['song_position'] - still['song_position'],
                slow_song_per_wall=(resumed['song_position'] - slow['song_position'])
                / ((resumed['guest_ns'] - slow['guest_ns']) / 1e9),
                paused_audio_dbfs=dbfs(paused_audio), playing_audio_dbfs=dbfs(playing_audio),
                paused_audio_dbfs_per_100ms=paused_profile,
                state_after=compact(game_state.observe(sandbox)))
            step = run_step(sandbox, {'id': 'tick-step', 'observe': ['stereo', 'state'], 'actions': [
                {'kind': 'game_speed', 'scale': 0}, {'kind': 'wait', 'ticks': 30}, {'kind': 'game_speed', 'scale': 1}]},
                state=game_state)
            timing['tick_step'] = {'acks': step['acknowledgements'], 'state': compact(step['observations']['state']['value']),
                                   'state_game_time_ns': step['observations']['state']['game_time_ns']}
            (args.output / 'game-time.json').write_text(json.dumps(timing, indent=1) + '\n')
            recording = sandbox.vr.record(args.output / 'recording', fps=30)
            with recording, Streams(sandbox, args.output) as streams, (args.output / 'inputs.jsonl').open('w') as inputs:
                begin = time.monotonic()
                while time.monotonic() - begin < args.seconds:
                    tick = time.monotonic()
                    action = trajectory(tick - begin)
                    response = sandbox.vr.action(action)
                    inputs.write(json.dumps({'elapsed_seconds': tick - begin,
                                             'action': action, 'ack': response}) + '\n')
                    time.sleep(max(0, 1 / 120 - (time.monotonic() - tick)))
            if streams.chunks[0]['first_frame'] != startup.chunks[-1]['next']:
                raise RuntimeError('gameplay audio does not continue the startup audio')
            gameplay_audio, anchor = audio_summary(streams.chunks, args.output / 'gameplay-audio.wav')
            video_start = recording.timeline[0]['capture_begin_ns']
            offset = (anchor - video_start) / 1e9
            audio_input = (['-itsoffset', f'{offset:.6f}'] if offset >= 0 else ['-ss', f'{-offset:.6f}'])
            muxed = args.output / 'recording/stereo-with-audio.mp4'
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', recording.metadata['videos']['stereo'],
                            *audio_input, '-i', str(args.output / 'gameplay-audio.wav'), '-map', '0:v', '-map', '1:a',
                            '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-shortest', str(muxed)], check=True)
            with (args.output / 'haptics.jsonl').open('w') as output:
                for event in startup.events + streams.events:
                    output.write(json.dumps(event) + '\n')
            window = {'start_ns': recording.timeline[0]['capture_begin_ns'],
                      'end_ns': recording.timeline[-1]['capture_begin_ns']}
            pulses = [e for e in streams.events if e['amplitude'] > 0]
            haptics = {'startup_events': len(startup.events), 'gameplay_events': len(streams.events),
                       'gameplay_pulses': len(pulses),
                       'pulses_by_device': {d: sum(e['device'] == d for e in pulses) for d in ('left', 'right')},
                       'pulses_in_capture_window': sum(window['start_ns'] <= e['guest_ns'] <= window['end_ns']
                                                       for e in pulses),
                       'amplitudes': sorted({round(e['amplitude'], 3) for e in pulses}),
                       'frequencies_hz': sorted({round(e['frequency_hz'], 1) for e in pulses}),
                       'durations_ns': sorted({e['duration_ns'] for e in pulses})}
            # The recorded frames at and 150 ms before the first pulses, to inspect what caused them.
            from sandweave.templates.vr.frames import RecordedFrames
            frames = RecordedFrames(args.output / 'recording')
            times = [meta['capture_begin_ns'] for meta in frames.metadata]
            chosen = []
            for pulse in pulses:
                if window['start_ns'] <= pulse['guest_ns'] and (not chosen or pulse['guest_ns'] - chosen[-1]['guest_ns'] > 1e9):
                    chosen.append(pulse)
            for index, pulse in enumerate(chosen[:4]):
                for label, at in (('before', pulse['guest_ns'] - 150_000_000), ('at', pulse['guest_ns'])):
                    nearest = min(range(len(times)), key=lambda i: abs(times[i] - at))
                    frames[nearest].image().save(args.output / f'haptic-{index}-{pulse["device"]}-{label}.png')
            # Unified steps: actions, then stereo, audio and haptics stamped on the guest clock.
            # Exporting the videos outlasts the 60 s audio window; continue from the current position.
            resumed = {'audio': sandbox.audio.observe(), 'haptics': sandbox.vr.haptics()}
            steps = []
            for index in range(40):
                state = trajectory(time.monotonic() - begin)
                result = run_step(sandbox, {'id': f'step-{index}', 'observe': ['stereo', 'audio', 'haptics'],
                                            'actions': [{'kind': 'xr_pose', 'device': device, **values}
                                                        for device, values in state.items()]})
                observed = result['observations']
                stereo, audio, felt = observed['stereo'], observed['audio']['value'], observed['haptics']['value']['vr']
                steps.append({'id': result['id'], 'input_guest_ack_ns': result['acknowledgements'][0]['guest_ack_ns'],
                              'stereo_game_time_ns': stereo['game_time_ns'],
                              'stereo_sequence': stereo['value'].metadata['sequence'],
                              'last_action_index': stereo['last_action_index'],
                              'audio': {k: audio[k] for k in ('first_frame', 'next', 'frames', 'dropped_frames',
                                                               'start_ns', 'end_ns')},
                              'audio_game_time_ns': observed['audio']['game_time_ns'],
                              'haptics': {k: felt[k] for k in ('first', 'next', 'guest_ns')},
                              'haptic_events': felt['events'],
                              'wall_ms': (result['finished_ns'] - result['started_ns']) / 1e6})
                if index == 39:
                    Image.fromarray(stereo['value'].left).save(args.output / 'step-left.png')
                    Image.fromarray(stereo['value'].right).save(args.output / 'step-right.png')
            (args.output / 'steps.jsonl').write_text(''.join(json.dumps(s) + '\n' for s in steps))
            step_checks = {
                'count': len(steps),
                'stereo_after_input': all(s['stereo_game_time_ns'] >= s['input_guest_ack_ns'] for s in steps),
                'audio_tiles': all(b['audio']['first_frame'] == a['audio']['next'] for a, b in zip(steps, steps[1:]))
                and steps[0]['audio']['first_frame'] == resumed['audio']['next'],
                'audio_dropped_frames': sum(s['audio']['dropped_frames'] for s in steps),
                'audio_dropped_during_video_export': resumed['audio']['dropped_frames'],
                'haptics_tile': all(b['haptics']['first'] == a['haptics']['next'] for a, b in zip(steps, steps[1:]))
                and steps[0]['haptics']['first'] == resumed['haptics']['next'],
                'haptic_pulses': sum(e['amplitude'] > 0 for s in steps for e in s['haptic_events']),
                'game_time_monotonic': all(b['stereo_game_time_ns'] > a['stereo_game_time_ns']
                                           for a, b in zip(steps, steps[1:])),
                'wall_ms_median': sorted(s['wall_ms'] for s in steps)[len(steps) // 2]}
            sandbox.files.download('/tmp/monado-metrics.pb', args.output / 'monado-metrics.pb')
            sandbox.files.download('/opt/vr/monado_metrics_pb2.py', args.output / 'monado_metrics_pb2.py')
            sys.path.insert(0, str(args.output.resolve()))
            spec = importlib.util.spec_from_file_location('vr_metrics', Path(__file__).with_name('vr-metrics.py'))
            metrics = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(metrics)
            result = {'recording': {**recording.metadata, 'stereo_with_audio': str(muxed)}, 'capture_window': window,
                      'audio': {'startup': startup_audio, 'gameplay': gameplay_audio,
                                'audio_offset_from_video_seconds': offset},
                      'haptics': haptics, 'steps': step_checks,
                      'game_time': {key: timing[key] for key in (
                          'paused_song_seconds', 'advance_ticks', 'advance_song_seconds', 'slow_song_per_wall',
                          'paused_audio_dbfs', 'playing_audio_dbfs')},
                      'game_state': {'before': timing['state_before'], 'after_time_tests': timing['state_after'],
                                     'after_recording': compact(game_state.observe(sandbox))},
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

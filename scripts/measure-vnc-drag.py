#!/usr/bin/env python3
"""Measure delivered visual updates during a real, repeatable VNC mouse drag."""
import argparse
import json
import math
from pathlib import Path
import statistics
import time

import numpy as np
from PIL import Image
from twisted.internet import reactor
from twisted.internet.defer import Deferred
from vncdotool import api, rfb
from vncdotool.client import VNCDoToolClient, VNCDoToolFactory
from vnc_zrle import handle as decode_zrle


class Recorder(VNCDoToolClient):
    _handleDecodeZRLEdata = decode_zrle
    recording = False
    received_bytes = 0

    def dataReceived(self, data):
        self.received_bytes += len(data)
        super().dataReceived(data)

    def measure(self, seconds, roi, center, amplitude):
        return self.refreshScreen().addCallback(
            lambda _: self.begin(seconds, roi, center, amplitude))

    def begin(self, seconds, roi, center, amplitude):
        self.done = Deferred()
        self.seconds, self.roi, self.center, self.amplitude = seconds, roi, center, amplitude
        self.previous = self.sample()
        if self.reference is not None:
            expected = np.asarray(self.reference.crop(roi), dtype=np.int16)[::4, ::4]
            difference = float(np.mean(np.abs(self.previous - expected)))
            if difference > 20:
                self.screen.save(self.failure_image)
                raise RuntimeError(f'Initial viewport differs from verified reference: mean channel error={difference:.2f}')
        self.baseline = self.previous.copy()
        self.samples, self.events = [], []
        self.snapshots = [(0.0, self.screen.copy())]
        self.last_snapshot = 0.0
        self.start_bytes = self.received_bytes
        self.start = time.perf_counter()
        self.mouseMove(*center)
        self.mouseDown(1)
        self.recording = True
        self.events.append({'seconds': 0.0, 'event': 'button_down'})
        for step in range(round(seconds * 20) + 1):
            reactor.callLater(0.25 + step / 20.0, self.move_step, step)
        reactor.callLater(seconds + 0.6, self.release)
        reactor.callLater(seconds + 2.0, self.finish)
        self.framebufferUpdateRequest(incremental=True)
        return self.done

    def sample(self):
        return np.asarray(self.screen.crop(self.roi), dtype=np.int16)[::4, ::4].copy()

    def move_step(self, step):
        if not self.recording:
            return
        elapsed = step / 20.0
        x = self.center[0] + round(self.amplitude * math.sin(math.pi * elapsed))
        self.mouseMove(x, self.center[1])
        self.events.append({'seconds': time.perf_counter() - self.start,
                            'scheduled_seconds': 0.25 + elapsed,
                            'event': 'move', 'x': x, 'y': self.center[1]})

    def release(self):
        self.mouseUp(1)
        self.events.append({'seconds': time.perf_counter() - self.start, 'event': 'button_up'})

    def commitUpdate(self, rectangles=None):
        if not self.recording:
            return super().commitUpdate(rectangles)
        elapsed = time.perf_counter() - self.start
        if rectangles:
            current = self.sample()
            delta = np.abs(current - self.previous)
            from_start = np.abs(current - self.baseline)
            self.samples.append({'seconds': elapsed,
                                 'changed_fraction': float(np.mean(np.max(delta, axis=2) > 8)),
                                 'baseline_changed_fraction': float(np.mean(np.max(from_start, axis=2) > 8)),
                                 'mean_channel_change': float(np.mean(delta))})
            self.previous = current
            if elapsed - self.last_snapshot >= 1:
                self.snapshots.append((elapsed, self.screen.copy()))
                self.last_snapshot = elapsed
        self.framebufferUpdateRequest(incremental=True)

    def finish(self):
        self.mouseUp(1)
        self.recording = False
        self.snapshots.append((time.perf_counter() - self.start, self.screen.copy()))
        self.done.callback({'samples': self.samples, 'events': self.events,
                            'received_bytes': self.received_bytes - self.start_bytes,
                            'snapshots': self.snapshots})


p = argparse.ArgumentParser()
p.add_argument('--port', type=int, required=True)
p.add_argument('--label', required=True)
p.add_argument('--output', type=Path, required=True)
p.add_argument('--seconds', type=float, default=6)
p.add_argument('--roi', type=int, nargs=4, default=[400, 180, 1100, 700])
p.add_argument('--center', type=int, nargs=2, default=[750, 450])
p.add_argument('--amplitude', type=int, default=80)
p.add_argument('--encoding', choices=['raw', 'zrle'], default='raw')
p.add_argument('--reference', type=Path)
a = p.parse_args()
assert a.seconds >= 2 and a.seconds % 2 == 0
a.output.mkdir(parents=True, exist_ok=False)
Recorder.encoding = rfb.Encoding.RAW if a.encoding == 'raw' else rfb.Encoding.ZRLE
Recorder.reference = Image.open(a.reference).convert('RGB') if a.reference else None
Recorder.failure_image = a.output / 'initial-reference-failure.png'


class Factory(VNCDoToolFactory):
    protocol = Recorder
    nocursor = True


try:
    with api.connect(f'127.0.0.1::{a.port}', password='labvnc01',
                     factory_class=Factory, timeout=a.seconds + 20) as client:
        result = client.measure(a.seconds, a.roi, a.center, a.amplitude)
finally:
    api.shutdown()

for index, (elapsed, frame) in enumerate(result.pop('snapshots')):
    frame.save(a.output / f'frame-{index:02d}-{elapsed:.3f}s.png')
motions = [e for e in result['events'] if e['event'] == 'move']
# The first sine-wave point is stationary; the second is the first real movement.
motion_start = motions[1]['seconds']
motion_end = motions[-1]['seconds']
changed = [s['seconds'] for s in result['samples']
           if motion_start <= s['seconds'] <= motion_end and s['changed_fraction'] >= .01]
first = next((s['seconds'] for s in result['samples']
              if s['seconds'] >= motion_start and s['baseline_changed_fraction'] >= .02), None)
gaps = [b - c for c, b in zip(changed, changed[1:])]
result['summary'] = {
    'label': a.label, 'encoding': a.encoding, 'roi': a.roi,
    'measurement': 'host wall clock; VNC delivered viewport updates, not engine FPS',
    'movement_start_seconds': motion_start, 'movement_end_seconds': motion_end,
    'first_visible_change_seconds': None if first is None else first - motion_start,
    'changed_updates': len(changed),
    'changed_updates_per_second': len(changed) / (motion_end - motion_start),
    'median_update_gap_seconds': statistics.median(gaps) if gaps else None,
    'largest_update_gap_seconds': max(gaps) if gaps else None,
    'movement_events': len(motions), 'received_bytes': result['received_bytes'],
    'largest_input_schedule_delay_seconds': max(e['seconds'] - e['scheduled_seconds'] for e in motions),
    'requires_visual_verification': True,
}
(a.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result['summary']), flush=True)
if first is None or len(changed) < 3:
    raise SystemExit('Insufficient visible movement; inspect the screenshots before interpreting this run.')

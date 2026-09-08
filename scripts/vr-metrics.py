#!/usr/bin/env python3
"""Summarize actual OpenXR submissions and compositor GPU work from Monado metrics.

Generate tools/gpu/vr/monado_metrics_pb2.py from the pinned upstream metrics
schema first; see notes/vr-monado-experiment.md. Captures may end with one
incomplete protobuf record while the service is still writing.
"""
import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import statistics
import sys

os.environ.setdefault('PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION', 'python')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tools/gpu/vr'))
import monado_metrics_pb2 as pb
from google.protobuf.internal.decoder import _DecodeVarint


def distribution(values):
    if not values:
        return None
    values = sorted(values)
    return {'median': statistics.median(values), 'p95': values[int(.95 * (len(values)-1))],
            'p99': values[int(.99 * (len(values)-1))], 'max': values[-1]}


def summarize(path, tail_seconds=30):
    data = Path(path).read_bytes()
    groups = defaultdict(list)
    offset = 0
    while offset < len(data):
        start = offset
        try:
            size, offset = _DecodeVarint(data, offset)
        except IndexError:
            offset = start
            break
        if offset + size > len(data):
            offset = start
            break
        record = pb.Record.FromString(data[offset:offset+size])
        offset += size
        kind = record.WhichOneof('record')
        if kind:
            groups[kind].append(getattr(record, kind))
    if not groups['version'] or groups['version'][0].major != 1:
        raise ValueError('unsupported or missing Monado metrics version')
    frames = [r for r in groups['session_frame'] if r.when_delivered_ns and not r.discarded]
    if len(frames) < 2:
        raise ValueError('fewer than two rendered application frames')
    end = max(r.when_delivered_ns for r in frames)
    session = max(frames, key=lambda r:r.when_delivered_ns).session_id
    start = end - tail_seconds * 1e9
    frames = sorted((r for r in frames if r.session_id == session and r.when_delivered_ns >= start),
                    key=lambda r:r.when_delivered_ns)
    if len(frames) < 2:
        raise ValueError('measurement window contains fewer than two application frames')
    start = frames[0].when_delivered_ns
    times = [r.when_delivered_ns for r in frames]
    gpu = [r for r in groups['system_gpu_info'] if start <= r.when_ns <= end]
    used = [r for r in groups['used'] if r.session_id == session and start <= r.when_ns <= end]
    system = [r for r in groups['system_frame'] if start <= r.wake_up_time_ns <= end]
    result = {'source': str(path), 'session_id': session, 'window_seconds': (end-start)/1e9,
              'application_frames': len(frames), 'application_fps': (len(frames)-1)*1e9/(end-start),
              'application_frame_interval_ms': distribution([(b-a)/1e6 for a,b in zip(times,times[1:])]),
              'compositor_frames': len(system),
              'compositor_gpu_ms': distribution([(r.gpu_end_ns-r.gpu_start_ns)/1e6 for r in gpu]),
              'used_records': len(used), 'unique_application_frames_used': len({r.session_frame_id for r in used}),
              'incomplete_trailing_bytes': len(data)-offset,
              'timing_ms': {},
              'scope': 'OpenXR frame submissions and GPU timestamps; no physical headset scanout or transport timing'}
    if used:
        result['reused_frame_fraction'] = 1 - len({r.session_frame_id for r in used})/len(used)
    if system:
        result['compositor_target_hz'] = 1e9 / statistics.median(r.predicted_display_period_ns for r in system)
    for key, a, b in (
        ('runtime_wait', 'when_predicted_ns', 'when_wait_woke_ns'),
        ('wait_return_to_begin', 'when_wait_woke_ns', 'when_begin_ns'),
        ('begin_to_end_frame', 'when_begin_ns', 'when_delivered_ns'),
        ('end_frame_to_gpu_completion_observed', 'when_delivered_ns', 'when_gpu_done_ns')):
        result['timing_ms'][key] = distribution([(getattr(r,b)-getattr(r,a))/1e6 for r in frames
                                                if getattr(r,b) and getattr(r,a)])
    result['timing_ms']['end_frame_to_next_wait'] = distribution([
        (b.when_predicted_ns-a.when_delivered_ns)/1e6 for a,b in zip(frames,frames[1:])])
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('metrics')
    parser.add_argument('--tail-seconds', type=float, default=30)
    args = parser.parse_args()
    if args.tail_seconds <= 0:
        parser.error('--tail-seconds must be positive')
    print(json.dumps(summarize(args.metrics, args.tail_seconds), indent=2))

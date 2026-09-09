#!/usr/bin/env python3
"""Align Monado application metrics to the actual saved stereo recording."""
import argparse
import importlib.util
import json
from pathlib import Path


def summarize_recording(recording, metrics):
    spec = importlib.util.spec_from_file_location('vr_metrics', Path(__file__).with_name('vr-metrics.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    rows = [json.loads(line) for line in (Path(recording) / 'frames.jsonl').read_text().splitlines()]
    start, end = rows[0]['capture_begin_ns'], rows[-1]['published_ns']
    data = Path(metrics).read_bytes()
    frames, system, offset = [], [], 0
    while offset < len(data):
        original = offset
        try:
            size, offset = module._DecodeVarint(data, offset)
        except IndexError:
            offset = original; break
        if offset+size > len(data):
            offset = original; break
        record = module.pb.Record.FromString(data[offset:offset+size]); offset += size
        kind = record.WhichOneof('record')
        if kind == 'session_frame' and record.session_frame.when_delivered_ns and not record.session_frame.discarded:
            frames.append(record.session_frame)
        elif kind == 'system_frame':
            system.append(record.system_frame.wake_up_time_ns)
    recorded = [f for f in frames if start <= f.when_delivered_ns <= end]
    if len(recorded) < 2:
        raise ValueError('recording and application metrics do not share a usable clock window')
    session = max(recorded, key=lambda f: f.when_delivered_ns).session_id
    result = {'metrics': str(metrics), 'recording': str(recording),
              'clock': 'guest monotonic timestamps from the compositor and Monado metrics',
              'recording_start_ns': start, 'recording_end_ns': end,
              'scope': 'OpenXR submissions during saved capture; no physical headset scanout',
              'incomplete_trailing_bytes': len(data)-offset, 'windows': {}}
    for label, begin in (('whole_recording', start), ('last_10_seconds_of_recording', max(start, end-10_000_000_000))):
        times = sorted(f.when_delivered_ns for f in recorded if f.session_id == session and f.when_delivered_ns >= begin)
        presented = sorted(t for t in system if begin <= t <= end)
        result['windows'][label] = {'application_frames': len(times),
            'application_fps': (len(times)-1)*1e9/(times[-1]-times[0]),
            'observed_application_seconds': (times[-1]-times[0])/1e9,
            'application_frame_interval_ms': module.distribution([(b-a)/1e6 for a,b in zip(times,times[1:])]),
            'compositor_frames': len(presented),
            'compositor_fps': (len(presented)-1)*1e9/(presented[-1]-presented[0]) if len(presented)>1 else None}
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--recording', type=Path, required=True)
    parser.add_argument('--metrics', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = summarize_recording(args.recording, args.metrics)
    args.output.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))

#!/usr/bin/env python3
"""Validate completed profiling receipts and summarize the measured trials."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics


def summarize(root):
    manifest = json.loads((root / 'manifest.json').read_text())
    summary = {'manifest': manifest, 'cases': {}}
    checksums = {}
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        metadata = json.loads((directory / 'metadata.json').read_text())
        receipt = json.loads((directory / 'complete.json').read_text())
        rows = [json.loads(line) for line in (directory / 'results.jsonl').read_text().splitlines()]
        if not rows or rows[-1] != {'event': 'complete'}:
            raise ValueError(f'{directory}: incomplete probe')
        if any(int(receipt['events'][k]) for k in ('oom', 'oom_kill', 'oom_group_kill')):
            raise ValueError(f'{directory}: out-of-memory event')
        if int(receipt['peak_bytes']) > metadata['limit_bytes']:
            raise ValueError(f'{directory}: exceeded requested physical-memory cap')
        size = metadata['size_mib'] * 1024**2
        prepared = next(row for row in rows if row['event'] == 'prepared')
        if not metadata['anonymous'] and (prepared['allocated_bytes'] < size or prepared['cached_bytes']):
            raise ValueError(f'{directory}: missing physical data or warm starting cache')
        samples = [json.loads(line) for line in (directory / 'memory.jsonl').read_text().splitlines()]
        if not samples or any(int(row['memory.swap.current']) for row in samples):
            raise ValueError(f'{directory}: missing telemetry or swap use')
        case = {'metadata': metadata, 'receipt': receipt, 'prepared': prepared,
                'samples': len(samples), 'phases': {}, 'evidence_sha256': {}}
        for name in ('metadata.json', 'complete.json', 'memory.jsonl', 'results.jsonl', 'stderr.log'):
            case['evidence_sha256'][name] = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        results = [row for row in rows if row['event'] == 'result']
        scan_checksum = next(row['checksum'] for row in results if row['bytes_scanned'])
        for row in results:
            if row['bytes_scanned'] and row['checksum'] != scan_checksum:
                raise ValueError(f'{directory}: streaming checksum mismatch')
            key = (row['phase'], row['repeat'])
            expected = checksums.setdefault(key, row['checksum'])
            if row['checksum'] != expected:
                raise ValueError(f'{directory}: checksum differs from other memory mode: {key}')
            if metadata['anonymous'] and (row['read_bytes'] or row['major_faults']):
                raise ValueError(f'{directory}: RAM baseline did physical reads')
            if directory.name == 'resident' and row['phase'] != 'cold_sequential':
                if row['read_bytes'] or row['cached_start_bytes'] != size:
                    raise ValueError(f'{directory}: resident baseline was not fully in RAM')
            if directory.name == 'overflow' and row['read_bytes'] == 0:
                raise ValueError(f'{directory}: disk case did not read physical storage')
        for phase in sorted({row['phase'] for row in results}):
            trials = [row for row in results if row['phase'] == phase]
            expected_count = 1 if phase in ('cold_sequential', 'first_sequential') else metadata['repeats']
            if len(trials) != expected_count:
                raise ValueError(f'{directory}: incomplete repetitions for {phase}')
            seconds = statistics.median(row['seconds'] for row in trials)
            item = {'trials': trials, 'median_seconds': seconds,
                    'min_seconds': min(row['seconds'] for row in trials),
                    'max_seconds': max(row['seconds'] for row in trials)}
            if trials[0]['bytes_scanned']:
                item['median_gib_per_second'] = size / 1024**3 / seconds
            else:
                operations = metadata['operations']
                item['median_us_per_operation'] = seconds * 1e6 / operations
                item['median_us_per_operation_including_flush'] = statistics.median(
                    row['seconds'] + row['flush_seconds'] for row in trials) * 1e6 / operations
            case['phases'][phase] = item
        summary['cases'][directory.name] = case
    if 'ram' in summary['cases']:
        baseline = summary['cases']['ram']['phases']
        for name, case in summary['cases'].items():
            for phase, item in case['phases'].items():
                if phase in baseline:
                    item['slowdown_vs_ram'] = item['median_seconds'] / baseline[phase]['median_seconds']
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', nargs='+', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    output = {root.name: summarize(root) for root in args.runs}
    args.output.write_text(json.dumps(output, indent=2) + '\n')
    print(f'Validated {sum(len(run["cases"]) for run in output.values())} cases; wrote {args.output}')


if __name__ == '__main__':
    main()

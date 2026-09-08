#!/usr/bin/env python3
"""One foreground status sample, including bytes in active partial files."""
import argparse
import datetime
import json
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--manifest', type=Path, default=Path('runs/alyx/windows-files.json'))
parser.add_argument('--destination', type=Path, default=Path('tools/gpu/alyx'))
parser.add_argument('--pid', type=int, required=True)
args = parser.parse_args()
entries = json.loads(args.manifest.read_text())
complete_bytes = partial_bytes = files = 0
mismatches = []
for entry in entries:
    target = args.destination / entry['path']
    partial = target.with_name(target.name + '.partial')
    try:
        size = target.stat().st_size
    except FileNotFoundError:
        try:
            partial_bytes += partial.stat().st_size
        except FileNotFoundError:
            pass  # An atomic rename can race this one status sample.
    else:
        if size == entry['size']:
            files += 1
            complete_bytes += size
        else:
            mismatches.append(entry['path'])
try:
    command = Path(f'/proc/{args.pid}/cmdline').read_bytes().split(b'\0')
    running = any(part.endswith(b'/import-alyx.py') for part in command)
except FileNotFoundError:
    running = False
total = sum(entry['size'] for entry in entries)
print(json.dumps({'utc': str(datetime.datetime.now(datetime.timezone.utc)),
                  'pid': args.pid, 'running': running, 'completed_files': files,
                  'total_files': len(entries), 'complete_bytes': complete_bytes,
                  'partial_bytes': partial_bytes, 'total_bytes': total,
                  'received_percent': round(100 * (complete_bytes + partial_bytes) / total, 2),
                  'size_mismatches': mismatches,
                  'all_files_complete': files == len(entries)}))

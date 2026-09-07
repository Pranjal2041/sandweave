#!/usr/bin/env python3
import json
from pathlib import Path
from statistics import median
root = Path(__file__).resolve().parent.parent
results = {}
for mode in ('native', 'uml'):
    rows = {}
    for path in sorted((root / 'runs/benchmarks').glob(f'{mode}-*.jsonl')):
        for line in path.read_text().splitlines():
            item = json.loads(line)
            rows.setdefault(item['test'], []).append(item)
    results[mode] = rows
summary = []
for test, native in results['native'].items():
    guest = results['uml'][test]
    assert len(native) == len(guest) == 3, (test, len(native), len(guest))
    if 'checksum' in native[0]:
        assert len({x['checksum'] for x in native + guest}) == 1, test
    n = median(x['seconds'] for x in native)
    u = median(x['seconds'] for x in guest)
    summary.append({'test': test, 'native_seconds': n, 'uml_seconds': u, 'ratio': u/n})
out = root / 'runs/benchmarks/summary.json'
out.write_text(json.dumps(summary, indent=2) + '\n')
print(out.read_text())

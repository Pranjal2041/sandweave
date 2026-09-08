#!/usr/bin/env python3
"""Compare real x86 signal frames with a disposable, networkless systrap sandbox."""
import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--allow-shadow-pte-bit', action='store_true',
                    help='permit only the known readonly-write page-present-bit discrepancy')
args = parser.parse_args()
lab = Path(__file__).resolve().parent.parent
local = Path((lab/'runs/local-path.txt').read_text().strip())
name = 'signal-context-' + str(time.time_ns())
logs = lab/'runs/gvisor'/name
logs.mkdir(parents=True)
binary = logs/'probe'
subprocess.run(['gcc', '-O0', '-Wall', '-Wextra', '-Werror',
                str(lab/'scripts/signal-context-probe.c'), '-o', str(binary)], check=True)
native = subprocess.check_output([str(binary)], timeout=20)
(logs/'native.jsonl').write_bytes(native)
subprocess.run(['python', str(lab/'scripts/make-gvisor-bundle.py'), name,
                '--', '/signal-context-probe'], check=True)
bundle = local/'gvisor/bundles'/name
with tarfile.open(bundle/'fixtures.tar', 'a') as archive:
    data = binary.read_bytes()
    entry = tarfile.TarInfo('./signal-context-probe')
    entry.mode, entry.size = 0o755, len(data)
    archive.addfile(entry, io.BytesIO(data))
command = ['taskset', '-c', str(min(os.sched_getaffinity(0))),
           str(lab/'scripts/gvisor-host.sh'), '/lab/tools/gvisor-socket/runsc',
           '--platform=systrap', '--systrap-disable-syscall-patching',
           '--network=none', '--ignore-cgroups', '--directfs=false', '--allow-suid',
           '--allow-rootfs-tar-annotation', '--sidecar-usage-policy=STRICT',
           '--root=/local/gvisor/state', 'run',
           '--bundle=/local/gvisor/bundles/'+name, name]
result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
(logs/'guest.jsonl').write_bytes(result.stdout)
(logs/'guest.stderr').write_bytes(result.stderr)
result.check_returncode()
native_rows = [json.loads(row) for row in native.splitlines()]
guest_rows = [json.loads(row) for row in result.stdout.splitlines()]
assert len(native_rows) == len(guest_rows) == 8
differences = []
for native_row, guest_row in zip(native_rows, guest_rows):
    for field in ('case', 'signal', 'code', 'trap', 'error'):
        if native_row[field] != guest_row[field]:
            differences.append({'case': native_row['case'], 'field': field,
                                'native': native_row[field], 'guest': guest_row[field]})
    if native_row['trap'] == 14:
        assert guest_row['address'] == guest_row['cr2'], guest_row
    else:
        assert guest_row['cr2'] == native_row['cr2'], guest_row
print(json.dumps({'cases': len(native_rows), 'differences': differences,
                  'exact_linux_match': not differences,
                  'evidence': str(logs)}, indent=2))
permitted = [{'case': 'readonly-write', 'field': 'error', 'native': 7, 'guest': 6}]
raise SystemExit(bool(differences) and not (args.allow_shadow_pte_bit and differences == permitted))

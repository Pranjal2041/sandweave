#!/usr/bin/env python3
"""Compare address/dispatch mechanisms in a disposable no-network systrap guest.

Run on the allocated compute node with taskset restricted to an allocated CPU.
This measures prerequisites; it does not boot Windows or measure VM performance.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import statistics
import subprocess
import tarfile
import time

lab = Path(__file__).resolve().parent.parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--runsc', type=Path, required=True)
parser.add_argument('--kernel-leaves', type=Path,
                    help='also support the generated NT leaf-routine blob as a separate experiment')
args = parser.parse_args()
runsc = args.runsc.resolve()
runsc_inside = '/lab/' + str(runsc.relative_to(lab))
if len(os.sched_getaffinity(0)) != 1:
    parser.error('pin this invocation to one CPU from your allocation with taskset')
local = Path((lab / 'runs/local-path.txt').read_text().strip())
name = ('windows-kernel-leaves-' if args.kernel_leaves else 'windows-address-') + str(time.time_ns())
logs = lab / 'runs/gvisor' / name
logs.mkdir(parents=True)
binary = logs / 'probe'
source = 'windows-kernel-routine-probe.c' if args.kernel_leaves else 'windows-native-address-probe.c'
subprocess.run(['gcc', '-O2', '-Wall', '-Wextra', '-Werror',
                str(lab / 'scripts' / source), '-o', str(binary)], check=True)
native_args = [str(args.kernel_leaves.resolve()), 'low'] if args.kernel_leaves else []
guest_args = ['/kernel-leaves.bin', 'low'] if args.kernel_leaves else []
native = subprocess.run([str(binary), *native_args], capture_output=True, timeout=130)
(logs / 'native.jsonl').write_bytes(native.stdout)
(logs / 'native.stderr').write_bytes(native.stderr)
native.check_returncode()
subprocess.run(['python', str(lab / 'scripts/make-gvisor-bundle.py'),
                name, '--', '/windows-address-probe', *guest_args], check=True)
bundle = local / 'gvisor/bundles' / name
config = json.loads((bundle / 'config.json').read_text())
config['annotations']['dev.gvisor.spec.rootfs.source'] = '/lab/images/gvisor-ubuntu-ready-ae303ca.erofs'
(bundle / 'config.json').write_text(json.dumps(config, indent=2) + '\n')
with tarfile.open(bundle / 'fixtures.tar', 'a') as archive:
    data = binary.read_bytes()
    entry = tarfile.TarInfo('./windows-address-probe')
    entry.mode, entry.size = 0o755, len(data)
    archive.addfile(entry, io.BytesIO(data))
    if args.kernel_leaves:
        data = args.kernel_leaves.read_bytes()
        entry = tarfile.TarInfo('./kernel-leaves.bin')
        entry.mode, entry.size = 0o444, len(data)
        archive.addfile(entry, io.BytesIO(data))
command = [str(lab / 'scripts/gvisor-host.sh'), runsc_inside,
           '--platform=systrap', '--systrap-disable-syscall-patching',
           '--network=none', '--ignore-cgroups', '--directfs=false', '--allow-suid',
           '--allow-rootfs-tar-annotation', '--sidecar-usage-policy=STRICT',
           '--root=/local/gvisor/state', 'run',
           '--bundle=/local/gvisor/bundles/' + name, name]
guest = subprocess.run(command, capture_output=True, timeout=160)
(logs / 'guest.jsonl').write_bytes(guest.stdout)
(logs / 'guest.stderr').write_bytes(guest.stderr)
summary = {'name': name, 'node': os.uname().nodename,
           'affinity': sorted(os.sched_getaffinity(0)),
           'runsc_sha256': hashlib.sha256(runsc.read_bytes()).hexdigest(),
           'probe_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
           'guest_exit': guest.returncode, 'evidence': str(logs), 'results': {}}
for kind, result in [('native', native), ('guest', guest)]:
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.startswith(b'{')]
    for row in rows:
        if 'samples' in row:
            row['median_shifted_over_native'] = statistics.median(
                sample['shifted_ns'] / sample['native_ns'] for sample in row['samples'])
        if 'elapsed_ns' in row:
            row['ns_per_dispatch'] = row['elapsed_ns'] / row['calls']
    summary['results'][kind] = rows
(logs / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
print(json.dumps(summary, indent=2))
guest.check_returncode()
expected_cases = ({'microsoft-nt-kernel-leaves'} if args.kernel_leaves else
                  {'low-alias', 'wide-alias', 'syscall-user-dispatch', 'seccomp-dispatch'})
for kind, rows in summary['results'].items():
    if len(rows) != len(expected_cases) or {row.get('case') for row in rows} != expected_cases:
        raise RuntimeError(f'{kind} probe did not report every expected case; see {logs}')

#!/usr/bin/env python3
"""Save a whole environment; verify its persistent copy in the background."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time
import uuid
import runtime_store
import snapshot_store

started = phase = time.perf_counter()
timings = {}


def mark(name):
    global phase
    now = time.perf_counter()
    timings[name] = now - phase
    phase = now


lab = Path(__file__).resolve().parent.parent
local = Path((lab / 'runs/local-path.txt').read_text().strip())
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--local-only', action='store_true', help='diagnostic snapshot in node-local storage only')
p.add_argument('name')
p.add_argument('checkpoint')
a = p.parse_args()
for name in (a.name, a.checkpoint):
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', name):
        p.error('names must use letters, digits, dash or underscore')
bundle = local / 'gvisor/bundles' / a.name
settings = json.loads((bundle / 'launch-settings.json').read_text())
runtime = settings['runtime']
runtime_root = runtime_store.validate(lab, runtime, verify=False)
state = json.loads((local / 'gvisor/state' / f'{a.name}_sandbox:{a.name}.state').read_text())
pid = state['sandbox']['pid']
if not os.path.samefile(f'/proc/{pid}/exe', runtime_root / 'gvisor-bin/gvisor_sentry'):
    raise ValueError('running Sentry does not match the recorded runtime; refusing an ambiguous snapshot')
dest = local / 'gvisor/checkpoints' / a.checkpoint
dest.mkdir(parents=True, exist_ok=False, mode=0o700)
published = lab / 'snapshots' / a.checkpoint
if published.exists():
    raise FileExistsError(published)
for source, target in [('config.json', 'lab-spec.json'), ('fixtures.tar', 'fixtures.tar'),
                       ('launch-settings.json', 'launch-settings.json'), ('network-policy.json', 'network-policy.json')]:
    shutil.copy2(bundle / source, dest / target)
spec = json.loads((dest / 'lab-spec.json').read_text())
base = spec['annotations']['dev.gvisor.spec.rootfs.source']
if base.startswith('/local/'):
    original_base = local / base.removeprefix('/local/')
elif base.startswith('/lab/'):
    original_base = lab / base.removeprefix('/lab/')
else:
    raise ValueError('unknown base-image location')
original_base = original_base.resolve()
base_stat = snapshot_store.signature(original_base)
# Persistent inputs are already shared. A legacy node-local input is copied
# once to a fresh name, then compared by the background verifier.
if original_base.is_relative_to(lab):
    persistent_base = original_base
else:
    persistent_base = lab / 'images' / (uuid.uuid4().hex + '.erofs')
    temporary_base = persistent_base.with_suffix('.partial')
    shutil.copy2(original_base, temporary_base)
    temporary_base.rename(persistent_base)
base_info = {'path': str(persistent_base.relative_to(lab)), 'size': base_stat['st_size']}
recorded_base = settings.get('base_image', {})
if recorded_base.get('path') == base_info['path'] and recorded_base.get('sha256'):
    base_info['sha256'] = recorded_base['sha256']
mark('input_setup_seconds')

suspensions = []
try:
    for registration in (local / 'gvisor/cpu-brokers').glob('*/job-' + a.name + '.json'):
        marker = registration.with_name(registration.name + '.suspend')
        marker.write_text('checkpoint in progress\n')
        suspensions.append(marker)
        deadline = time.monotonic() + 5
        while True:
            status = json.loads((registration.parent / 'status.json').read_text())
            job = status['jobs'].get(registration.name)
            if job and not job['paused'] and status['time'] > marker.stat().st_mtime:
                break
            if time.monotonic() > deadline:
                raise TimeoutError('CPU controller did not release checkpoint scheduling')
            time.sleep(.05)
    command = [str(lab / 'scripts/gvisor-host.sh'), '/lab/' + str(runtime_root.relative_to(lab)) + '/runsc',
               '--root=/local/gvisor/state', 'checkpoint', '--leave-running',
               '--image-path=/local/gvisor/checkpoints/' + a.checkpoint, a.name]
    mark('controller_release_seconds')
    start = time.perf_counter()
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
    pause_seconds = time.perf_counter() - start
    (dest / 'checkpoint.log').write_bytes(result.stdout)
    print(result.stdout.decode(errors='replace'), end='')
    result.check_returncode()
finally:
    for marker in suspensions:
        marker.unlink(missing_ok=True)
mark('checkpoint_seconds')

captured = {f.name: snapshot_store.signature(f) for f in dest.iterdir() if f.is_file()}
files = {name: {'size': stat['st_size']} for name, stat in captured.items()}
assert 'checkpoint.img' in files and 'pages.img' in files
manifest = {'format': 2, 'snapshot_id': uuid.uuid4().hex, 'container': a.name, 'pause_seconds': pause_seconds,
            'runtime': runtime, 'base_image': base_info, 'files': files,
            'verification_source': {'hostname': socket.gethostname(), 'path': str(dest),
                                    'files': captured, 'base_path': str(original_base), 'base_stat': base_stat},
            'external_connections': 'Host transport connections reconnect after restore; saved internal virtual networks remain inside the snapshot.'}
snapshot_store.write_json(dest / 'snapshot-manifest.json', manifest)
snapshot_store.pending(dest, manifest)
mark('manifest_seconds')
if not a.local_only:
    published.parent.mkdir(exist_ok=True)
    temporary = published.with_name('.' + published.name + '.publishing')
    shutil.copytree(dest, temporary)
    temporary.rename(published)
else:
    published = dest
mark('publication_seconds')
verifier = snapshot_store.start_verification(lab, published)
mark('verifier_start_seconds')
timings['total_seconds'] = time.perf_counter() - started
snapshot_store.write_json(published / 'save-timings.json', timings)
print(json.dumps({'snapshot': str(published), 'pause_seconds': pause_seconds,
                  'saved_bytes': sum(f['size'] for f in files.values()), 'runtime': runtime['path'],
                  'verification': str(published / 'verification.json'), 'verifier_pid': verifier.pid,
                  'timings': timings}, indent=2))

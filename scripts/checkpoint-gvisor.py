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
import filesystem_snapshot
import environment_control
import fast_io
from environment import EnvironmentManager

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
p.add_argument('--filesystem', action='store_true', help='save persistent filesystems; restore boots fresh processes')
p.add_argument('--experimental-gpu-live', action='store_true', help='diagnostic CUDA-aware live snapshot; graphics is not qualified')
p.add_argument('name')
p.add_argument('checkpoint')
a = p.parse_args()
for name in (a.name, a.checkpoint):
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', name):
        p.error('names must use letters, digits, dash or underscore')
operation_lock = environment_control.acquire_lock(local, a.name, os.environ.get(environment_control.LOCK_FD_ENV))
bundle = local / 'gvisor/bundles' / a.name
settings = json.loads((bundle / 'launch-settings.json').read_text())
if a.filesystem and a.experimental_gpu_live:
    p.error('choose filesystem or experimental live capture')
if a.experimental_gpu_live and not settings.get('gpu'):
    p.error('--experimental-gpu-live requires a GPU environment')
if settings.get('gpu', {}).get('mps'):
    p.error('MPS snapshots are not qualified')
if settings.get('gpu') and not (a.filesystem or a.experimental_gpu_live):
    p.error('use --filesystem for GPU environments; live GPU capture needs --experimental-gpu-live')
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
if not a.filesystem:
    # A cold-restored guest starts from its saved upper archive, not the small
    # launch fixture tar. Live restore must retain that exact immutable input.
    upper = spec['annotations']['dev.gvisor.tar.rootfs.upper']
    if upper.startswith('/local/'):
        upper_source = local / upper.removeprefix('/local/')
    elif upper.startswith('/lab/'):
        upper_source = lab / upper.removeprefix('/lab/')
    else:
        raise ValueError('unknown rootfs upper input location')
    if not os.path.samefile(upper_source, bundle / 'fixtures.tar'):
        shutil.copy2(upper_source, dest / 'fixtures.tar')
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

with environment_control.suspended_cpu(local, a.name):
    # Exec-donated frame FDs cannot be restored. Detach them from Xvnc and
    # reap the helper before freezing; the next I/O call reconnects lazily.
    fast_io.detach(EnvironmentManager(lab), a.name)
    command = [str(lab / 'scripts/gvisor-host.sh')]
    if settings.get('gpu'):
        command += ['--gpu', str(settings['gpu']['device_minor'])]
    command += ['/lab/' + str(runtime_root.relative_to(lab)) + '/runsc', '--root=/local/gvisor/state']
    mark('controller_release_seconds')
    start = time.perf_counter()
    filesystem = None
    if a.filesystem:
        filesystem = filesystem_snapshot.capture(command, a.name, dest, spec, lab, local)
    else:
        extra = ['--cuda-checkpoint-path=/opt/engine-gpu/bin/cuda-checkpoint', '--cuda-checkpoint-sequential'] if a.experimental_gpu_live else []
        result = subprocess.run([*command, 'checkpoint', '--leave-running', *extra,
                                 '--image-path=/local/gvisor/checkpoints/' + a.checkpoint, a.name],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
        (dest / 'checkpoint.log').write_bytes(result.stdout)
        print(result.stdout.decode(errors='replace'), end='')
        result.check_returncode()
    pause_seconds = time.perf_counter() - start
mark('checkpoint_seconds')

captured = {f.name: snapshot_store.signature(f) for f in dest.iterdir() if f.is_file()}
files = {name: {'size': stat['st_size']} for name, stat in captured.items()}
required = {'rootfs-upper.tar'} if a.filesystem else {'checkpoint.img', 'pages.img'}
if not required <= files.keys():
    raise ValueError('capture did not produce its required payloads')
manifest = {'format': 2, 'kind': 'filesystem' if a.filesystem else 'live',
            'experimental_gpu_live': a.experimental_gpu_live, 'filesystem': filesystem,
            'snapshot_id': uuid.uuid4().hex, 'container': a.name, 'pause_seconds': pause_seconds,
            'runtime': runtime, 'base_image': base_info, 'files': files,
            'verification_source': {'hostname': socket.gethostname(), 'path': str(dest),
                                    'files': captured, 'base_path': str(original_base), 'base_stat': base_stat},
            'external_connections': ('Fresh network stack on cold boot.' if a.filesystem else
                                     'Host transport connections reconnect after restore; saved internal virtual networks remain inside the snapshot.')}
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

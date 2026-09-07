#!/usr/bin/env python3
"""Save a durable, checksummed recovery point for the lab implementation."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import runtime_store

lab = Path(__file__).resolve().parent.parent
local = Path((lab / 'runs/local-path.txt').read_text().strip())
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('label')
a = p.parse_args()
if not a.label or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in a.label):
    p.error('label must contain letters, digits, dash or underscore')
dest = lab / 'checkpoints' / a.label
repo = lab / 'sources/gvisor'
status = subprocess.check_output(['git', 'status', '--porcelain'], cwd=repo, text=True)
if status:
    raise SystemExit('Commit source changes before checkpointing: ' + status)
descriptor = json.loads((lab / 'tools/gvisor-socket/runtime.json').read_text())
runtime = runtime_store.validate(lab, descriptor)
dest.mkdir(parents=True, exist_ok=False, mode=0o700)
commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
subprocess.run(['git', 'bundle', 'create', str(dest / 'gvisor.bundle'), '--all'], cwd=repo, check=True)
subprocess.run(['git', 'bundle', 'verify', str(dest / 'gvisor.bundle')], cwd=repo, check=True)
shallow = repo / '.git/shallow'
if shallow.exists():
    shutil.copy2(shallow, dest / 'gvisor.shallow')
with tarfile.open(dest / 'lab-code-and-notes.tar.gz', 'w:gz') as archive:
    for path in [lab / 'README.md', lab / 'scripts', lab / 'notes']:
        archive.add(path, arcname=str(path.relative_to(lab)),
                    filter=lambda entry: None if '__pycache__' in entry.name else entry)
shutil.copytree(runtime, dest / 'runtime')
(dest / 'runtime/runtime.json').write_text(json.dumps(descriptor, indent=2) + '\n')
shutil.copytree(lab / 'images/fixtures', dest / 'fixtures')
(dest / 'guest-tools').mkdir()
for name in ('bench', 'seccomp-trap', 'gs-base-probe'):
    shutil.copy2(lab / 'tools' / name, dest / 'guest-tools' / name)
shutil.copy2(shutil.which('passt'), dest / 'passt')
rootfs = lab / 'images/gvisor-ubuntu-ready-ae303ca.erofs'
if not rootfs.exists():
    temp = rootfs.with_suffix('.erofs.partial')
    subprocess.run(['cp', '--reflink=auto', '--sparse=always', str(local / 'gvisor/ubuntu-ready.erofs'), str(temp)], check=True)
    temp.replace(rootfs)
evidence = dest / 'evidence'
evidence.mkdir()
for path in sorted((lab / 'runs').glob('gvisor*.log')):
    shutil.copy2(path, evidence / path.name)
for pattern in ('resource-isolation-status.json', 'moodle-user1/ports.json', 'earth-user1/ports.json',
                'moodle-user1/user-ready.png', '*/isolation-status.out', 'earth-user1/drag-zrle*/**/*.json',
                '*/*.json', '*/*.jsonl', '*/policy-live.out', '*/oom-recovery.out', '*/snapshot-*.png'):
    for path in sorted((lab / 'runs/gvisor').glob(pattern)):
        if path.is_file():
            target = evidence / path.relative_to(lab / 'runs/gvisor')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
for relative in ('heapguard1/boot.log', 'snapdocker9-restored/boot.log',
                 'snapdocker10-restored/boot.log', 'snapdocker11-restored/boot.log'):
    source = lab / 'runs/gvisor' / relative
    if source.is_file():
        target = evidence / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(8 * 1024**2), b''):
            h.update(chunk)
    return {'bytes': path.stat().st_size, 'sha256': h.hexdigest()}

manifest = {'created_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'source_commit': commit, 'hostname': os.uname().nodename,
            'kind': 'implementation-recovery-point; not a running-environment snapshot',
            'runtime': descriptor,
            'files': {}, 'persistent_dependencies': {}}
for path in sorted(dest.rglob('*')):
    if path.is_file():
        manifest['files'][str(path.relative_to(dest))] = digest(path)
for path in [rootfs, lab / 'images/gvisor-moodle-persisted-docker.tar', lab / 'tools/debian-trixie.sif',
             lab / 'tools/gvisor-builder.sif']:
    print('Hashing', path, flush=True)
    manifest['persistent_dependencies'][str(path.relative_to(lab))] = digest(path)
(dest / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
(dest / 'RECOVERY.md').write_text(
    f'Source commit: {commit}\n\n'
    'This saves the implementation and existing test evidence. It does not freeze the running desktops.\n\n'
    'Verify SHA256 values in manifest.json before recovery. For a shallow bundle, initialize sources/gvisor '
    'with git init, copy gvisor.shallow to sources/gvisor/.git/shallow, then run git fetch <absolute-path>/gvisor.bundle '
    'refs/heads/experiment/no-kvm-slurm:refs/heads/experiment/no-kvm-slurm from that repository. '
    'Checkout experiment/no-kvm-slurm and verify the recorded commit. Without gvisor.shallow, a shallow '
    'source bundle cannot be cloned offline. Extract lab-code-and-notes.tar.gz into the lab. Copy runtime/ '
    f'to both tools/gvisor-socket/ and {descriptor["path"]}/, preserving executable modes. '
    'Restore fixtures/ to images/fixtures/ and guest-tools/ to tools/. Put the saved passt executable '
    'on PATH (for example tools/bin/passt and export PATH="$PWD/tools/bin:$PATH"). '
    'Use the recorded persistent SIF files and Docker archive. '
    'Copy the recorded EROFS image to <local>/gvisor/ubuntu-ready.erofs and set runs/local-path.txt. '
    'Follow notes/gvisor-lab-reproduction.md and notes/resource-snapshot-implementation.md; '
    'ports and node-local paths change on a new allocation. Running snapshots remain separate '
    'under snapshots/ and require all dependencies listed in their snapshot-manifest.json.\n')
print(json.dumps({'checkpoint': str(dest), 'source_commit': commit}, indent=2), flush=True)

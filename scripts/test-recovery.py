#!/usr/bin/env python3
"""Reconstruct a separate lab from a recovery bundle and restore its desktop."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import runtime_store

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('checkpoint')
a = p.parse_args()
lab = Path(__file__).resolve().parent.parent
checkpoint = lab / 'checkpoints' / a.checkpoint
manifest = json.loads((checkpoint / 'manifest.json').read_text())
for name, info in manifest['files'].items():
    assert runtime_store.digest(checkpoint / name) == info['sha256'], name
for name, info in manifest['persistent_dependencies'].items():
    assert runtime_store.digest(lab / name) == info['sha256'], name
recovered = lab / 'runs' / ('recovery-' + str(time.time_ns()))
recovered.mkdir(mode=0o700)
with tarfile.open(checkpoint / 'lab-code-and-notes.tar.gz') as source:
    source.extractall(recovered, filter='data')
for name in manifest['persistent_dependencies']:
    target = recovered / name
    target.parent.mkdir(parents=True, exist_ok=True)
    os.link(lab / name, target)
shutil.copytree(checkpoint / 'runtime', recovered / 'tools/gvisor-socket')
shutil.copytree(checkpoint / 'runtime', recovered / manifest['runtime']['path'])
shutil.copytree(checkpoint / 'fixtures', recovered / 'images/fixtures')
for source in (checkpoint / 'guest-tools').iterdir():
    shutil.copy2(source, recovered / 'tools' / source.name)
(recovered / 'tools/bin').mkdir()
shutil.copy2(checkpoint / 'passt', recovered / 'tools/bin/passt')
(recovered / 'sources').mkdir()
repo = recovered / 'sources/gvisor'
if (checkpoint / 'gvisor.shallow').exists():
    subprocess.run(['git', 'init', '--quiet', str(repo)], check=True)
    shutil.copy2(checkpoint / 'gvisor.shallow', repo / '.git/shallow')
    subprocess.run(['git', 'fetch', '--quiet', str(checkpoint / 'gvisor.bundle'),
                    'refs/heads/experiment/no-kvm-slurm:refs/heads/experiment/no-kvm-slurm'], cwd=repo, check=True)
    subprocess.run(['git', 'checkout', '--quiet', 'experiment/no-kvm-slurm'], cwd=repo, check=True)
else:
    subprocess.run(['git', 'clone', '--quiet', str(checkpoint / 'gvisor.bundle'), str(repo)], check=True)
head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=recovered / 'sources/gvisor', text=True).strip()
assert head == manifest['source_commit'], head
snapshot = recovered / 'snapshots/full-desktop-ready'
snapshot.mkdir(parents=True)
for source in (lab / 'snapshots/full-desktop-ready').iterdir():
    if source.is_file():
        os.link(source, snapshot / source.name)
local = Path(tempfile.mkdtemp(prefix='general-vm-recovery.'))
(local / 'gvisor-tmp').mkdir()
(recovered / 'runs').mkdir(exist_ok=True)
(recovered / 'runs/local-path.txt').write_text(str(local) + '\n')
env = dict(os.environ, PATH=str(recovered / 'tools/bin') + ':' + os.environ['PATH'])
name = 'recovered-desktop'
subprocess.run(['python', 'scripts/run-gvisor.py', '--detach', '--cpus', '4-7', '--restore', str(snapshot), name], cwd=recovered, env=env, check=True)
logs = recovered / 'runs/gvisor' / name
result = None
try:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if (logs / 'exit-code.txt').exists():
            raise RuntimeError((logs / 'launcher.out').read_text()[-4000:])
        try:
            ports = json.loads((logs / 'ports.json').read_text())
            with urllib.request.urlopen('http://127.0.0.1:' + str(ports['8000']), timeout=3) as response:
                probe = json.load(response)
            break
        except (OSError, ValueError):
            time.sleep(.5)
    else:
        raise TimeoutError('fresh recovery did not become ready')
    assert probe['nonce'] == probe['cross_netns_tcp'] == probe['marker']
    assert probe['unlinked_file'] == 'open-file:' + probe['nonce'] and probe['file_offset'] == 5
    assert probe['abstract_queued'] == 'queued-before-checkpoint'
    prefix = ['scripts/gvisor-host.sh', '/lab/' + manifest['runtime']['path'] + '/runsc', '--root=/local/gvisor/state', 'exec', name]
    web = subprocess.check_output(prefix + ['curl', '--max-time', '10', '-s', '-o', '/dev/null', '-w', '%{http_code}', 'http://localhost/login/index.php'], cwd=recovered, env=env, timeout=20)
    assert web == b'200', web
    nested = subprocess.check_output(prefix + ['docker', 'exec', 'general-vm-moodle', 'docker', 'ps', '--format', '{{.Names}} {{.Status}}'], cwd=recovered, env=env, timeout=20)
    assert b'moodle-mariadb Up' in nested, nested
    with socket.create_connection(('127.0.0.1', ports['5901']), timeout=5) as connection:
        banner = connection.recv(12).decode().strip()
    assert banner.startswith('RFB '), banner
    result = {'passed': True, 'recovered_lab': str(recovered), 'fresh_local_storage': str(local),
              'source_commit': head, 'probe': probe, 'moodle_http': 200, 'nested_docker': nested.decode(),
              'vnc_banner': banner, 'inputs': 'Recovery archive and recorded persistent immutable dependencies; separate bundles, state, networking and CPU broker.'}
    (lab / 'runs/gvisor-recovery-acceptance.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
finally:
    control = local / 'gvisor/state' / ('runsc-' + name + '.sock')
    if control.exists():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(str(control))
            connection.sendall(json.dumps({'method': 'containerManager.Signal', 'arg': {'CID': name, 'Signo': 9, 'PID': 0, 'Mode': 1}}).encode())
            connection.recv(8192)

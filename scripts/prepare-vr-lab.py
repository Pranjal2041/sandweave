#!/usr/bin/env python3
"""Prepare pinned Monado and Open Saber in a disposable, already running GPU sandbox.

Downloads/builds stay in ignored directories. All apt/build commands run inside
the sandbox, through its pinned gVisor runtime. No host sudo or KVM is used.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request

from environment import EnvironmentManager

MONADO = 'f8dfadfeaeb46df3eec17bd76b7abdf42a79108c'
METRICS = '41e64fa19837534028c6db89ea641b4cc1552b3c'
GAME = 'OpenSaber0.5.0.Linux.x86_64'
GAME_SHA = 'adc189b96d7321f9c7d142c9243c244394c3ad35a95b8a7fdf0ea4779b74e526'


def checkout(path, url, revision):
    if not path.exists():
        subprocess.run(['git', 'clone', '--depth=1', url, str(path)], check=True)
    status = subprocess.check_output(['git', '-C', str(path), 'status', '--porcelain'], text=True)
    head = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
    if status:
        raise RuntimeError(f'preserve modified dependency checkout: {path}')
    if head != revision:
        subprocess.run(['git', '-C', str(path), 'fetch', '--depth=1', 'origin', revision], check=True)
    subprocess.run(['git', '-C', str(path), 'checkout', '--detach', revision], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name')
    args = parser.parse_args()
    manager = EnvironmentManager()
    state = manager.status(args.name)
    if not args.name.startswith('vr-') or state['status'] != 'running' or not state.get('gpu'):
        parser.error('select a running disposable GPU sandbox named vr-*')
    prefix = [*manager._command(args.name), 'exec', args.name]
    if manager._run([*prefix, 'sh', '-c', 'pgrep -x monado-service || true']).strip():
        parser.error('stop the VR experiment before preparing its files')
    lab = manager.lab
    downloads = lab / 'downloads/vr'
    stage = lab / 'tools/gpu/vr'
    downloads.mkdir(parents=True, exist_ok=True)
    stage.mkdir(parents=True, exist_ok=True)
    checkout(downloads/'monado', 'https://gitlab.freedesktop.org/monado/monado.git', MONADO)
    checkout(downloads/'metrics', 'https://gitlab.freedesktop.org/monado/utilities/metrics.git', METRICS)
    game = stage/GAME
    if not game.exists():
        temporary = game.with_suffix('.partial')
        with urllib.request.urlopen('https://github.com/leandrodreamer/BeepSaber/releases/download/v0.5.0/'+GAME,
                                    timeout=60) as response, temporary.open('wb') as destination:
            shutil.copyfileobj(response, destination)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != GAME_SHA:
            raise RuntimeError('published game download hash mismatch')
        temporary.replace(game)
    if hashlib.sha256(game.read_bytes()).hexdigest() != GAME_SHA:
        raise RuntimeError('published game binary hash mismatch')
    game.chmod(0o755)
    vulkan = json.loads((lab/'tools/gpu/driver/vulkan.json').read_text())
    vulkan['ICD']['library_path'] = '/opt/engine-gpu/driver/lib/libGLX_nvidia.so.0'
    (stage/'vulkan.json').write_text(json.dumps(vulkan, indent=2)+'\n')
    archive = stage/'monado-source.tar'
    with archive.open('wb') as stream:
        subprocess.run(['git', '-C', str(downloads/'monado'), 'archive', '--format=tar',
                        '--prefix=monado-source/', MONADO], stdout=stream, check=True)
    subprocess.run([*prefix, 'mkdir', '-p', '/opt/vr'], check=True)
    with archive.open('rb') as stream:
        subprocess.run([*prefix, 'tar', '-C', '/opt/vr', '-xf', '-'], stdin=stream, check=True)
    payload = stage/'payload.tar'
    with tarfile.open(payload, 'w') as archive:
        for path in (game, stage/'vulkan.json', lab/'notes/monado-vr-lab.patch',
                     downloads/'metrics/proto/monado_metrics.proto'):
            archive.add(path, arcname=path.name)
        for name in ('vr-monado-build.sh', 'vr-monado-guest.sh', 'vr-remote-input.py', 'vr_input.py', 'vr_stream_guest.py'):
            archive.add(lab/'scripts'/name, arcname=name)
    with payload.open('rb') as stream:
        subprocess.run([*prefix, 'tar', '-C', '/opt/vr', '-xf', '-'], stdin=stream, check=True)
    # Route output through the parent: runsc's passed file descriptors can have
    # independent offsets when stdout is a redirected host regular file.
    with subprocess.Popen([*prefix, 'sh', '/opt/vr/vr-monado-build.sh'], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True) as process:
        for line in process.stdout:
            print(line, end='', flush=True)
        if process.wait():
            raise subprocess.CalledProcessError(process.returncode, process.args)
    with (stage/'monado_metrics_pb2.py').open('wb') as stream:
        subprocess.run([*prefix, 'cat', '/opt/vr/monado_metrics_pb2.py'], stdout=stream, check=True)
    print(json.dumps({'prepared': args.name, 'monado': MONADO, 'game_sha256': GAME_SHA,
                      'patch': 'notes/monado-vr-lab.patch'}))


if __name__ == '__main__':
    main()

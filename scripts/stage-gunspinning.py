#!/usr/bin/env python3
"""Stage the official Linux archive and pinned lab dependencies (no game edits).

Obtain gunspinning-vr-linux.zip through https://demonixis.itch.io/gunspinning-vr
using its free/name-your-price download. Download credentials are not stored here.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

GAME_SHA = '85c440f22f16fcbeec018b0f4be4df8bb3b3ac091a3eda83f43cf2c8d06ad392'
PRIMUS_REV = '7076c2e6a55cfc7c292eb68ca17b00dae498ef81'
PRIMUS_SHA = '23e3c50ed7d65b684a02b8ebe61a82d0749176b09f8e9869c3e3f40ce0ec7ca1'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def readable(root):
    for path in [root, *root.rglob('*')]:
        if not path.is_symlink():
            path.chmod(path.stat().st_mode | (0o555 if path.is_dir() else 0o444))


def main():
    lab = Path(__file__).resolve().parent.parent
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive', type=Path,
                   default=lab / 'downloads/gunspinning-vr/gunspinning-vr-linux.zip')
    args = p.parse_args()
    if not args.archive.is_file() or digest(args.archive) != GAME_SHA:
        p.error('supply the official Linux 2.0.1 archive; required SHA256: ' + GAME_SHA)
    target = lab / 'tools/gpu/vr'
    target.mkdir(parents=True, exist_ok=True)
    game = target / 'gunspinning-linux-2.0.1'
    if not game.exists():
        with tempfile.TemporaryDirectory(prefix='gunspinning-', dir=target) as temporary:
            with zipfile.ZipFile(args.archive) as archive:
                for member in archive.namelist():
                    if Path(member).is_absolute() or '..' in Path(member).parts:
                        raise ValueError('unsafe archive member')
                archive.extractall(temporary)
            executable = Path(temporary) / 'GunSpinningVR'
            executable.chmod(0o755)
            readable(Path(temporary))
            Path(temporary).rename(game)
    if not (game / 'GunSpinningVR').is_file():
        raise RuntimeError('unexpected Linux archive layout')
    cache = lab / 'downloads/gunspinning-vr'
    cache.mkdir(parents=True, exist_ok=True)
    archive_path = cache / 'primus-vk-7076c2e.tar.gz'
    url = 'https://codeload.github.com/felixdoerre/primus_vk/tar.gz/' + PRIMUS_REV
    if not archive_path.exists():
        archive_path.write_bytes(urllib.request.urlopen(url, timeout=30).read())
    if digest(archive_path) != PRIMUS_SHA:
        raise RuntimeError('Primus-VK source archive hash mismatch')
    source = target / 'gunspinning-primus-source'
    if not source.exists():
        with tempfile.TemporaryDirectory(prefix='primus-', dir=target) as temporary:
            with tarfile.open(archive_path) as archive:
                archive.extractall(temporary, filter='data')
            tree = Path(temporary) / ('primus_vk-' + PRIMUS_REV)
            subprocess.run(['patch', '-p1', '-i', str(lab / 'notes/primus-vk-visible-rows.patch')],
                           cwd=tree, check=True)
            readable(tree)
            tree.rename(source)
    subprocess.run([sys.executable, str(lab / 'scripts/build-sdl-gamepad-proxy.py')], check=True)
    readable(target / 'libsdl-gamepad-proxy.so')
    (target / 'gunspinning-provenance.json').write_text(json.dumps({
        'game_version': 'Linux 2.0.1', 'game_archive_sha256': GAME_SHA,
        'primus_revision': PRIMUS_REV, 'primus_archive_sha256': PRIMUS_SHA,
        'primus_patch_sha256': digest(lab / 'notes/primus-vk-visible-rows.patch'),
        'game_assemblies_modified': False,
    }, indent=2) + '\n')
    print('Staged assets under', target)


if __name__ == '__main__':
    main()

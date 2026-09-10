#!/usr/bin/env python3
"""Package a verified engine build; publish this script's outputs through Releases."""
import argparse
import gzip
import json
from pathlib import Path
import shutil
import tarfile
import tempfile

from sandweave.bootstrap import build_input
from sandweave.installation import record_installation
from sandweave.releases import validate_engine
from sandweave.sandbox import workspace


def package(build_result, output, version, repository, notices):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    result = json.loads(Path(build_result).read_text())
    source = Path(result['assets'])
    descriptor = result['runtime']
    name = 'sandweave-runtime-' + version + '-linux-x86_64.tar.gz'
    archive = output / name
    if archive.exists() or (output / 'manifest.json').exists():
        raise ValueError('Release outputs already exist; choose a new directory or version')
    with tempfile.TemporaryDirectory(prefix='.package-', dir=output) as temporary:
        root = Path(temporary) / 'runtime'
        root.mkdir()
        shutil.copytree(source / descriptor['path'], root / descriptor['path'])
        if not (notices / 'sources.json').is_file():
            raise ValueError('Collect runtime notices before packaging the release')
        shutil.copytree(notices, root / 'licenses')
        workspace.atomic_json(root / 'tools/gvisor-socket/runtime.json', descriptor)
        shutil.copy2(build_input('gvisor-no-kvm-prototype.patch'), root / 'engine.patch')
        workspace.atomic_json(root / 'source.json', {
            'upstream': 'https://github.com/google/gvisor',
            'upstream_commit': result['upstream_commit'],
            'engine_patch_sha256': result['patch_sha256'],
            'builder_sha256': result['builder_sha256'],
            'build_script': 'scripts/build-runtime-release.py',
            'version': version})
        record_installation(root)
        validate_engine(root)
        # Fixed ownership, timestamps and archive ordering do not expose builder
        # paths/user IDs and make repackaging the same input byte-reproducible.
        unpacked = 0
        with archive.open('xb') as output_file, gzip.GzipFile(filename='', mode='wb', fileobj=output_file,
                mtime=0, compresslevel=6) as compressed, tarfile.open(fileobj=compressed, mode='w|') as stream:
            for path in [root, *sorted(root.rglob('*'))]:
                member = stream.gettarinfo(str(path), arcname='runtime/' + str(path.relative_to(root)))
                member.uid = member.gid = member.mtime = 0
                member.uname = member.gname = ''
                if path.is_file():
                    member.type, member.linkname = tarfile.REGTYPE, ''
                    member.size = path.stat().st_size
                    member.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                    unpacked += member.size
                    with path.open('rb') as data:
                        stream.addfile(member, data)
                elif path.is_dir():
                    member.mode = 0o755
                    stream.addfile(member)
                else:
                    raise ValueError('Engine release contains a non-regular entry: ' + str(path))
    base_url = 'https://github.com/' + repository + '/releases/download/runtime-v' + version + '/'
    artifact = {'architecture': 'x86_64', 'minimum_kernel': '5.6', 'cpu_flags': ['sse2'],
                'name': name, 'url': base_url + name, 'size': archive.stat().st_size,
                'sha256': workspace.file_digest(archive), 'unpacked_bytes': unpacked}
    manifest = {'schema_version': 1, 'version': version, 'engine_patch_sha256': result['patch_sha256'],
                'upstream_commit': result['upstream_commit'], 'artifacts': [artifact]}
    workspace.atomic_json(output / 'manifest.json', manifest)
    workspace.atomic_json(output / 'runtime-release.json', {'schema_version': 1, 'manifest': {
        'url': base_url + 'manifest.json', 'sha256': workspace.file_digest(output / 'manifest.json')}})
    (output / 'SHA256SUMS').write_text(''.join(workspace.file_digest(path) + '  ' + path.name + '\n'
        for path in (archive, output / 'manifest.json', output / 'runtime-release.json')))
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-result', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--version', required=True)
    parser.add_argument('--repository', default='Pranjal2041/sandweave')
    parser.add_argument('--notices', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(package(args.build_result, args.output, args.version, args.repository, args.notices), indent=2))

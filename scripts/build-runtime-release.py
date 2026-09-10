#!/usr/bin/env python3
"""Build a clean coding runtime for a binary release, without changing user setup."""
import argparse
import json
from pathlib import Path
import shutil

from sandweave.bootstrap import Builder, BUILDER, GVISOR_BASE, build_input
from sandweave.installation import using_directory
from sandweave.sandbox import workspace
from sandweave.templates.resolve import Template


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path, help='empty build directory; prefer local scratch')
    parser.add_argument('--result', required=True, type=Path, help='write build provenance and the resulting path here')
    parser.add_argument('--image-cache', type=Path, help='optional trusted local directory with existing builder SIF files')
    args = parser.parse_args()
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        parser.error('build directory must be empty')
    if args.result.exists():
        parser.error('result file already exists')
    with using_directory(directory):
        if args.image_cache:
            downloads = directory / 'downloads'
            downloads.mkdir()
            for name, reference in [('debian-trixie.sif', 'docker://debian:trixie-slim'),
                                    ('gvisor-builder.sif', BUILDER)]:
                source = args.image_cache / name
                if source.is_file():
                    shutil.copy2(source, downloads / name)
                    workspace.atomic_json((downloads / name).with_suffix('.image.json'),
                        {'reference': reference, 'sha256': workspace.file_digest(downloads / name)})
        assets = Builder(directory).build('coding', Template('coding').resolve())
        result = {'assets': str(assets), 'upstream_commit': GVISOR_BASE,
                  'patch_sha256': workspace.file_digest(build_input('gvisor-no-kvm-prototype.patch')),
                  'builder_sha256': workspace.file_digest(assets / 'tools/gvisor-builder.sif'),
                  'runtime': json.loads((assets / 'tools/gvisor-socket/runtime.json').read_text())}
        args.result.parent.mkdir(parents=True, exist_ok=True)
        workspace.atomic_json(args.result, result)
        print('Release build ready: ' + str(assets), flush=True)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Create a Modal VM-runtime worker that can launch runtime="modal" sandboxes.

The worker runs Sandweave's ordinary (gVisor-capable) worker on Modal's VM runtime
and receives Modal credentials only from the named Modal Secret. It installs the
given wheel with the modal and vr extras and prepares the coding installation.

    scripts/modal-runtime-worker.py --wheel dist/sandweave-*.whl [--secret sandweave-modal-credentials]

Requires Modal credentials on the client; MODAL_ENVIRONMENT selects the environment.
"""
import argparse
import json
import os
from pathlib import Path
import sys

import modal

APP = 'sandweave-modal-runtime-lab'
HOME = '/home/sw'
PACKAGES = ('python3 python3-pip python3-venv git curl ca-certificates libglib2.0-0 libgomp1 file uidmap squashfuse '
            'squashfs-tools fuse3 fuse2fs fuse-overlayfs iproute2 procps kmod util-linux ffmpeg')


def run(sandbox, *argv, user=True):
    prefix = ['setpriv', '--reuid=sw', '--regid=sw', '--init-groups', 'env', f'HOME={HOME}',
              f'SANDWEAVE_HOME={HOME}/sandweave', 'PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin'] if user else []
    process = sandbox.exec(*prefix, *argv, timeout=3 * 3600, workdir=HOME)
    for line in process.stdout:
        sys.stdout.write(line)
    sys.stderr.write(process.stderr.read())
    if process.wait():
        raise SystemExit(f'command failed: {argv}')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--secret', default='sandweave-modal-credentials')
    parser.add_argument('--hours', type=float, default=12)
    args = parser.parse_args()
    image = (modal.Image.from_registry('ubuntu:24.04').apt_install(*PACKAGES.split())
             .run_commands('useradd -m sw', 'python3 -m venv /opt/venv', f'install -d -o sw -g sw {HOME}/sandweave'))
    app = modal.App.lookup(APP, create_if_missing=True)
    environment = {'MODAL_ENVIRONMENT': os.environ.get('MODAL_ENVIRONMENT', '')}
    sandbox = modal.Sandbox.create('sleep', 'infinity', app=app, image=image, cpu=8, memory=32768,
                                   timeout=int(args.hours * 3600), secrets=[modal.Secret.from_name(args.secret)],
                                   env={k: v for k, v in environment.items() if v},
                                   experimental_options={'vm_runtime': True})
    remote = '/tmp/' + args.wheel.name
    sandbox.filesystem.write_bytes(args.wheel.read_bytes(), remote)
    run(sandbox, 'bash', '-c', f'chown sw /dev/fuse 2>/dev/null; chmod 600 /dev/fuse 2>/dev/null; '
                               f'/opt/venv/bin/pip install --quiet "{remote}[modal,vr]"', user=False)
    run(sandbox, 'sandweave', 'setup', '--template', 'coding', '--yes')
    print(json.dumps({'sandbox': sandbox.object_id, 'wheel': args.wheel.name}))


if __name__ == '__main__':
    main()

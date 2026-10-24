#!/usr/bin/env python3
"""Create a disposable Modal GPU worker (Modal's gVisor sandbox) with this checkout's wheel, then run commands on it.

Modal is test infrastructure here, as QEMU is for linux-kernel-vm.py; it is not a Sandweave dependency.
Modal GPU sandboxes run under Modal's own gVisor (``uname -r`` reports ``4.19.0-gvisor``) and cannot use
Modal's VM runtime. Requires the ``modal`` package and credentials on the client.

    scripts/modal-gpu-worker.py create --wheel dist/sandweave-*.whl [--gpu A10] [--hours 6]
    scripts/modal-gpu-worker.py run SANDBOX_ID -- bash -lc 'sandweave doctor --check'
    scripts/modal-gpu-worker.py put SANDBOX_ID LOCAL REMOTE
    scripts/modal-gpu-worker.py get SANDBOX_ID REMOTE LOCAL
    scripts/modal-gpu-worker.py copy SOURCE_SANDBOX DESTINATION_SANDBOX SOURCE DESTINATION
    scripts/modal-gpu-worker.py terminate SANDBOX_ID
"""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import sys

import modal

APP = 'sandweave-gpu-lab'
HOME = '/home/sw'
ENV = {'HOME': HOME, 'SANDWEAVE_HOME': HOME + '/sandweave', 'PATH': '/opt/venv/bin:/usr/local/bin:/usr/bin:/bin'}
AS_SW = ['setpriv', '--reuid=sw', '--regid=sw', '--init-groups', 'env', *(f'{k}={v}' for k, v in ENV.items())]
PACKAGES = ('python3 python3-pip python3-venv git curl ca-certificates libglib2.0-0 libgomp1 file uidmap squashfuse '
            'squashfs-tools fuse3 fuse2fs fuse-overlayfs iproute2 procps kmod util-linux strace')


def image():
    return (modal.Image.from_registry('ubuntu:24.04').apt_install(*PACKAGES.split())
            .run_commands('useradd -m sw', 'python3 -m venv /opt/venv', f'install -d -o sw -g sw {HOME}/sandweave'))


def put(sandbox, local, remote):
    data = Path(local).read_bytes()
    sandbox.filesystem.write_bytes(data, remote)
    digest = sandbox.exec('sha256sum', remote).stdout.read().split()[0]
    if digest != hashlib.sha256(data).hexdigest():
        raise SystemExit(f'upload of {local} to {remote} does not match')


def run(sandbox, argv, root=False):
    process = sandbox.exec(*([] if root else AS_SW), *argv, timeout=24 * 3600)
    for line in process.stdout:
        sys.stdout.write(line)
    sys.stdout.flush()
    sys.stderr.write(process.stderr.read())
    return process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('create')
    create.add_argument('--wheel', type=Path, required=True)
    create.add_argument('--gpu', default='A10')
    create.add_argument('--cpu', type=float, default=8)
    create.add_argument('--memory-gib', type=int, default=32)
    create.add_argument('--hours', type=float, default=6)
    for name in ('run', 'put', 'get', 'copy', 'terminate'):
        p = sub.add_parser(name)
        p.add_argument('sandbox')
        if name == 'run':
            p.add_argument('--root', action='store_true')
            p.add_argument('argv', nargs=argparse.REMAINDER)
        if name == 'put':
            p.add_argument('local')
            p.add_argument('remote')
        if name == 'get':
            p.add_argument('remote')
            p.add_argument('local')
        if name == 'copy':
            p.add_argument('destination_sandbox')
            p.add_argument('source')
            p.add_argument('destination')
    args = parser.parse_args()
    if args.command == 'create':
        app = modal.App.lookup(APP, create_if_missing=True)
        # --gpu none: a Modal VM-runtime CPU worker (real kernel), e.g. for engine builds.
        placement = ({'experimental_options': {'vm_runtime': True}} if args.gpu == 'none' else {'gpu': args.gpu})
        sandbox = modal.Sandbox.create('sleep', 'infinity', app=app, image=image(), cpu=args.cpu,
                                       memory=args.memory_gib * 1024, timeout=int(args.hours * 3600), **placement)
        remote = '/tmp/' + args.wheel.name
        put(sandbox, args.wheel, remote)
        if run(sandbox, ['bash', '-c', f'chown sw /dev/fuse 2>/dev/null; chmod 600 /dev/fuse 2>/dev/null; '
                         f'/opt/venv/bin/pip install --quiet {shlex.quote(remote)}[desktop]'], root=True):
            raise SystemExit('wheel installation failed')
        print(json.dumps({'sandbox': sandbox.object_id, 'gpu': args.gpu, 'wheel': args.wheel.name}))
        return 0
    sandbox = modal.Sandbox.from_id(args.sandbox)
    if args.command == 'put':
        put(sandbox, args.local, args.remote)
        return 0
    if args.command in ('get', 'copy'):
        source = args.remote if args.command == 'get' else args.source
        data = sandbox.filesystem.read_bytes(source)
        digest = hashlib.sha256(data).hexdigest()
        expected = sandbox.exec('sha256sum', source).stdout.read().split()[0]
        if digest != expected:
            raise SystemExit('source file changed during download')
        if args.command == 'get':
            with Path(args.local).open('xb') as output:
                output.write(data)
        else:
            destination = modal.Sandbox.from_id(args.destination_sandbox)
            destination.filesystem.write_bytes(data, args.destination)
            actual = destination.exec('sha256sum', args.destination).stdout.read().split()[0]
            if actual != digest:
                raise SystemExit('destination checksum mismatch')
        print(json.dumps({'bytes': len(data), 'sha256': digest}))
        return 0
    if args.command == 'terminate':
        sandbox.terminate()
        return 0
    argv = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
    return run(sandbox, argv, root=args.root)


if __name__ == '__main__':
    sys.exit(main())

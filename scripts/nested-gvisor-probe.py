#!/usr/bin/env python3
"""Run an isolated OCI guest through Sandweave's engine on a gVisor worker.

Use an unused evidence directory. A detached launch is not proof that the guest
ran: acceptance requires the attached command's output and exit status.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--runtime', required=True, type=Path)
    parser.add_argument('--image', required=True, type=Path)
    parser.add_argument('--erofs', required=True, type=Path)
    parser.add_argument('--apptainer', required=True, type=Path)
    parser.add_argument('--storage', choices=('memory', 'disk'), default='memory')
    parser.add_argument('--platform', choices=('systrap', 'ptrace'), default='systrap')
    parser.add_argument('--gpu', type=int, help='host NVIDIA device minor')
    parser.add_argument('--fixture', type=Path, action='append', default=[])
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    with args.erofs.open('rb') as image:
        image.seek(1024)
        if image.read(4) != b'\xe2\xe1\xf5\xe0':
            parser.error('--erofs must be an EROFS filesystem image')
    root = args.directory.resolve()
    root.mkdir()  # Deliberately reject an existing experiment.
    for name in ('logs', 'state', 'tmp', 'bundle/rootfs', 'store', 'share'):
        (root / name).mkdir(parents=True)
    for fixture in args.fixture:
        shutil.copy2(fixture, root / 'share' / fixture.name)
    command = args.command or ['/bin/sh', '-c', 'echo NESTED_GUEST_OK; uname -r']
    if command[0] == '--':
        command = command[1:]
    overlay = 'memory' if args.storage == 'memory' else 'dir=/sandbox-storage'
    spec = {
        'ociVersion': '1.0.0',
        'process': {
            'user': {'uid': 0, 'gid': 0}, 'args': command,
            'env': ['PATH=/usr/local/bin:/usr/bin:/bin'], 'cwd': '/',
            'capabilities': {key: ['CAP_SYS_ADMIN'] for key in ('bounding', 'effective', 'permitted')},
        },
        'root': {'path': 'rootfs', 'readonly': False},
        'hostname': 'nested-probe',
        'mounts': [
            {'destination': '/proc', 'type': 'proc', 'source': 'proc'},
            {'destination': '/dev', 'type': 'tmpfs', 'source': 'tmpfs'},
            {'destination': '/share', 'type': 'bind', 'source': '/probe/share', 'options': ['rbind', 'rw']},
        ],
        'linux': {
            'namespaces': [{'type': name} for name in ('pid', 'ipc', 'uts', 'mount', 'network', 'user')],
            'uidMappings': [{'containerID': 0, 'hostID': os.getuid(), 'size': 1}],
            'gidMappings': [{'containerID': 0, 'hostID': os.getgid(), 'size': 1}],
        },
        'annotations': {
            'dev.gvisor.spec.rootfs.source': '/erofs/root.erofs',
            'dev.gvisor.spec.rootfs.type': 'erofs',
            'dev.gvisor.spec.rootfs.overlay': overlay,
        },
    }
    gpu_binds = []
    runtime_flags = []
    if args.gpu is not None:
        from sandweave.sandbox.workspace import engine_sources
        sys.path.insert(0, str(engine_sources()))
        import gvisor_gpu

        devices = gvisor_gpu.allocated_device(args.gpu)
        identity = gvisor_gpu.device_identity(args.gpu)
        gvisor_gpu.stage_driver(root / 'gpu/driver')
        spec['linux']['devices'] = devices
        spec['mounts'].append({'destination': '/opt/engine-gpu', 'type': 'bind',
                               'source': '/probe/gpu', 'options': ['bind', 'ro']})
        spec['process']['env'] += [
            'LD_LIBRARY_PATH=/opt/engine-gpu/driver/lib',
            f'NVIDIA_VISIBLE_DEVICES={identity["uuid"]}', 'CUDA_VISIBLE_DEVICES=0',
            'NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics,video',
        ]
        spec['annotations']['dev.gvisor.internal.nvproxy'] = 'true'
        spec['annotations'].update({
            'dev.gvisor.spec.mount.gpu.source': '/probe/gpu',
            'dev.gvisor.spec.mount.gpu.type': 'bind',
            'dev.gvisor.spec.mount.gpu.share': 'container',
            'dev.gvisor.spec.mount.gpu.options': 'ro',
        })
        for device in devices:
            gpu_binds += ['--bind', device['path']]
        runtime_flags = ['--nvproxy', '--nvproxy-allow-unsupported-driver',
                         '--nvproxy-driver-version=' + gvisor_gpu.driver_version(),
                         '--nvproxy-allowed-driver-capabilities=compute,utility,graphics,video']
    (root / 'bundle/config.json').write_text(json.dumps(spec, indent=2) + '\n')
    argv = [
        str(args.apptainer), 'exec', '--userns', '--contain', '--ipc', '--cleanenv', '--no-home',
        *gpu_binds,
        '--bind', f'{args.runtime.resolve()}:/runtime',
        '--bind', f'{root}:/probe',
        '--bind', f'{root / "tmp"}:/tmp',
        '--bind', f'{root / "store"}:/sandbox-storage',
        '--bind', f'{args.erofs.resolve()}:/erofs/root.erofs',
        str(args.image), '/runtime/runsc', '--network=none', f'--platform={args.platform}',
        '--directfs=false', '--ignore-cgroups', '--debug', '--debug-log=/probe/logs/',
        '--root=/probe/state', f'--overlay2=root:{overlay}', '--systrap-disable-syscall-patching',
        *runtime_flags,
        'run', '--bundle=/probe/bundle', 'nested-probe',
    ]
    (root / 'command.json').write_text(json.dumps(argv, indent=2) + '\n')
    start = time.monotonic()
    result = subprocess.run(argv, capture_output=True, text=True)
    (root / 'stdout.txt').write_text(result.stdout)
    (root / 'stderr.txt').write_text(result.stderr)
    (root / 'output.txt').write_text(result.stdout + result.stderr)
    receipt = {'returncode': result.returncode, 'seconds': time.monotonic() - start,
               'kernel': os.uname().release, 'storage': args.storage, 'platform': args.platform}
    (root / 'result.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt), flush=True)
    print((root / 'output.txt').read_text(), end='')
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
import argparse
import io
import json
import os
from pathlib import Path
import re
import tarfile

lab = Path(__file__).resolve().parent.parent
local = Path((lab / 'runs/local-path.txt').read_text().strip())
parser = argparse.ArgumentParser()
parser.add_argument('--docker-data', action='store_true')
parser.add_argument('name')
parser.add_argument('command', nargs=argparse.REMAINDER)
args = parser.parse_args()
if not re.fullmatch(r'[a-zA-Z0-9_-]+', args.name):
    parser.error('name must contain only letters, digits, dash, or underscore')
bundle = local / 'gvisor' / 'bundles' / args.name
bundle.mkdir(parents=True, exist_ok=False)
(bundle / 'rootfs').mkdir(exist_ok=True)
with tarfile.open(bundle / 'fixtures.tar', 'w') as archive:
    for name in ['./', './etc/', './usr/', './usr/local/', './usr/local/bin/']:
        entry = tarfile.TarInfo(name)
        entry.type = tarfile.DIRTYPE
        entry.mode, entry.uid, entry.gid = 0o755, 0, 0
        archive.addfile(entry)
    fixtures = [
        (lab / 'tools/bench', 'usr/local/bin/engine-bench'),
        (lab / 'tools/seccomp-trap', 'usr/local/bin/engine-seccomp-trap'),
        (lab / 'tools/gs-base-probe', 'usr/local/bin/engine-gs-base-probe'),
        (lab / 'scripts/build_artifacts.py', 'usr/local/bin/engine-build-artifacts'),
    ] + [(p, 'usr/local/bin/engine-' + p.stem.removeprefix('gvisor-guest-'))
         for p in sorted((lab / 'scripts').glob('gvisor-guest-*.sh'))]
    for source, dest in fixtures:
        if not source.is_file():
            # Optional lab probes are absent from public runtime releases.
            continue
        data = source.read_bytes()
        entry = tarfile.TarInfo('./' + dest)
        entry.mode, entry.uid, entry.gid, entry.size = 0o755, 0, 0, len(data)
        archive.addfile(entry, io.BytesIO(data))
    for name, data in {
        './etc/hostname': b'general-vm\n',
        './etc/hosts': b'127.0.0.1 localhost general-vm\n::1 localhost\n',
        './etc/resolv.conf': b'nameserver 10.0.2.3\noptions timeout:2 attempts:2\n',
    }.items():
        entry = tarfile.TarInfo(name)
        entry.mode, entry.uid, entry.gid, entry.size = 0o644, 0, 0, len(data)
        archive.addfile(entry, io.BytesIO(data))
# OCI names for Linux's capability ABI, through CAP_CHECKPOINT_RESTORE (40).
# These describe the guest. Launching does not require host development headers.
caps = ['CAP_' + name for name in (
    'CHOWN DAC_OVERRIDE DAC_READ_SEARCH FOWNER FSETID KILL SETGID SETUID SETPCAP '
    'LINUX_IMMUTABLE NET_BIND_SERVICE NET_BROADCAST NET_ADMIN NET_RAW IPC_LOCK '
    'IPC_OWNER SYS_MODULE SYS_RAWIO SYS_CHROOT SYS_PTRACE SYS_PACCT SYS_ADMIN '
    'SYS_BOOT SYS_NICE SYS_RESOURCE SYS_TIME SYS_TTY_CONFIG MKNOD LEASE '
    'AUDIT_WRITE AUDIT_CONTROL SETFCAP MAC_OVERRIDE MAC_ADMIN SYSLOG WAKE_ALARM '
    'BLOCK_SUSPEND AUDIT_READ PERFMON BPF CHECKPOINT_RESTORE').split()]
command = args.command or ['/usr/local/bin/engine-gates']
if command[0] == '--':
    command = command[1:]
spec = {
    'ociVersion': '1.2.1',
    'root': {'path': 'rootfs', 'readonly': False},
    'hostname': 'general-vm',
    'process': {
        'terminal': False, 'user': {'uid': 0, 'gid': 0},
        'args': command, 'cwd': '/',
        'env': ['PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                'HOME=/root', 'LANG=C.UTF-8', 'TERM=xterm', 'container=gvisor'],
        'capabilities': {key: caps for key in ['bounding', 'effective', 'inheritable', 'permitted', 'ambient']},
        'noNewPrivileges': False,
        'rlimits': [{'type': 'RLIMIT_NOFILE', 'hard': 131072, 'soft': 131072}],
    },
    'mounts': [
        {'destination': '/proc', 'type': 'proc', 'source': 'proc'},
        {'destination': '/dev', 'type': 'tmpfs', 'source': 'tmpfs', 'options': ['mode=755']},
        {'destination': '/dev/pts', 'type': 'devpts', 'source': 'devpts', 'options': ['newinstance', 'ptmxmode=0666', 'mode=0620']},
        {'destination': '/dev/shm', 'type': 'tmpfs', 'source': 'tmpfs', 'options': ['mode=1777', 'size=2g']},
        {'destination': '/run', 'type': 'tmpfs', 'source': 'tmpfs', 'options': ['mode=755']},
        {'destination': '/tmp', 'type': 'tmpfs', 'source': 'tmpfs', 'options': ['mode=1777']},
        {'destination': '/sys', 'type': 'sysfs', 'source': 'sysfs'},
        {'destination': '/sys/fs/cgroup', 'type': 'cgroup', 'source': 'cgroup', 'options': ['rw']},
    ],
    'linux': {
        'namespaces': [{'type': n} for n in ['pid', 'ipc', 'uts', 'mount', 'network', 'user', 'cgroup']],
        'uidMappings': [{'containerID': 0, 'hostID': os.getuid(), 'size': 1}],
        'gidMappings': [{'containerID': 0, 'hostID': os.getgid(), 'size': 1}],
        'resources': {'cpu': {'quota': 400000, 'period': 100000}, 'memory': {'limit': 8 * 1024**3}},
    },
    'annotations': {
        'dev.gvisor.spec.rootfs.source': '/local/gvisor/ubuntu-ready.erofs',
        'dev.gvisor.spec.rootfs.type': 'erofs',
        'dev.gvisor.spec.rootfs.overlay': 'memory',
        'dev.gvisor.tar.rootfs.upper': f'/local/gvisor/bundles/{args.name}/fixtures.tar',
    },
}
if args.docker_data:
    spec['mounts'].extend([
        {'destination': '/var/lib/docker', 'type': 'tmpfs', 'source': 'tmpfs', 'options': ['mode=0711']},
        {'destination': '/var/lib/containerd', 'type': 'tmpfs', 'source': 'tmpfs', 'options': ['mode=0711']},
    ])
(bundle / 'config.json').write_text(json.dumps(spec, indent=2) + '\n')
print(bundle)

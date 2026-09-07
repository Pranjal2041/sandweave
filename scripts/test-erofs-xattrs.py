#!/usr/bin/env python3
"""Build a real EROFS fixture and compare native Linux with the lab engine."""
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tarfile
import time

lab = Path(__file__).resolve().parent.parent
local = Path((lab / 'runs/local-path.txt').read_text().strip())
name = 'xattrs-' + str(time.time_ns())
root = local / 'gvisor' / name
root.mkdir(mode=0o755)
root.chmod(0o755)  # The cluster umask otherwise blocks the named test user.
named_uid = 1000 if os.getuid() != 1000 else 1001
public = root / 'public'
public.write_bytes(b'file payload\x00end')
os.setxattr(public, 'user.binary', b'a\x00b\xff')
os.setxattr(public, 'user.empty', b'')
for i in range(4):
    path = root / f'shared{i}'
    path.write_text('shared file\n')
    os.setxattr(path, 'user.common', b'shared-value' * 128)
for filename, mask in [('acl-allow', 4), ('acl-deny', 0)]:
    path = root / filename
    path.write_text('named ACL file\n')
    os.setxattr(path, 'user.probe', b'acl-value')
    entries = [(1, 6, 0xffffffff), (2, 4, named_uid), (4, 0, 0xffffffff),
               (16, mask, 0xffffffff), (32, 0, 0xffffffff)]
    acl = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *entry) for entry in entries)
    os.setxattr(path, 'system.posix_acl_access', acl)
(root / 'link').symlink_to('public')
image = root.with_suffix('.erofs')
subprocess.run([str(lab / 'tools/erofs-native/usr/bin/mkfs.erofs'), '--quiet', '-x1',
                str(image), str(root)], check=True)
logs = lab / 'runs/gvisor' / name
logs.mkdir()
native = subprocess.check_output([sys.executable, str(lab / 'scripts/probe-erofs-xattrs.py'), str(root)])
(logs / 'native.json').write_bytes(native)


def run(label, uid, gid, program, argv):
    sandbox = name + '-' + label
    subprocess.run([sys.executable, str(lab / 'scripts/make-gvisor-bundle.py'),
                    sandbox, '--', 'python3', '/probe.py', *argv], check=True)
    bundle = local / 'gvisor/bundles' / sandbox
    config = json.loads((bundle / 'config.json').read_text())
    config['process']['user'] = {'uid': uid, 'gid': gid}
    config['process']['capabilities'] = {key: [] for key in config['process']['capabilities']}
    config['mounts'].append({'destination': '/fixture', 'type': 'erofs', 'options': ['ro'],
                             'source': '/local/' + str(image.relative_to(local))})
    (bundle / 'config.json').write_text(json.dumps(config, indent=2) + '\n')
    with tarfile.open(bundle / 'fixtures.tar', 'a') as archive:
        info = tarfile.TarInfo('./probe.py')
        info.mode, info.size = 0o644, len(program)
        archive.addfile(info, io.BytesIO(program))
    cpu = str(min(os.sched_getaffinity(0)))
    cmd = ['taskset', '-c', cpu, str(lab / 'scripts/gvisor-host.sh'),
           '/lab/tools/gvisor-socket/runsc', '--platform=systrap',
           '--systrap-disable-syscall-patching', '--network=none', '--ignore-cgroups',
           '--directfs=false', '--allow-suid', '--allow-rootfs-tar-annotation',
           '--sidecar-usage-policy=STRICT', '--root=/local/gvisor/state',
           'run', '--bundle=/local/gvisor/bundles/' + sandbox, sandbox]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (logs / (label + '.out')).write_bytes(result.stdout)
    if result.returncode:
        print(result.stdout.decode(), file=sys.stderr)
        result.check_returncode()
    return result.stdout


guest = run('native-match', os.getuid(), os.getgid(),
            (lab / 'scripts/probe-erofs-xattrs.py').read_bytes(), ['/fixture'])
assert json.loads(guest) == json.loads(native)
acl_probe = b'''import os,errno
from pathlib import Path
p=Path('/fixture')
assert (p/'acl-allow').read_text()=='named ACL file\\n'
assert os.getxattr(p/'acl-allow','user.probe')==b'acl-value'
for operation in (lambda: (p/'acl-deny').read_bytes(), lambda: os.getxattr(p/'acl-deny','user.probe')):
 try: operation(); raise AssertionError('ACL mask bypassed')
 except OSError as e: assert e.errno==errno.EACCES,e
assert 'system.posix_acl_access' in os.listxattr(p/'acl-deny')
assert len(os.getxattr(p/'acl-deny','system.posix_acl_access'))==44
try: os.setxattr(p/'public','user.binary',b'new'); raise AssertionError('Writable EROFS')
except OSError as e: assert e.errno==errno.EROFS,e
print('Named ACL grant/mask denial, metadata visibility, read-only enforcement PASS')
'''
run('named-acl', named_uid, named_uid, acl_probe, [])
print('Native/guest data, path/fd xattrs, empty/binary/shared values, sizes and errno MATCH')
print('Named user ACL grant/mask denial and read-only enforcement PASS')
print('Evidence:', logs)

#!/usr/bin/env python3
"""Exercise persistent metadata/content and overlay deletion across a cold boot."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('action', choices=['seed', 'check', 'mutate'])
a = p.parse_args()
root = Path('/opt/fs-state')
attrs = {'user.binary': b'\x00\xff\x80abc', 'user.empty': b'', 'user.name=equals': b'kept'}
if a.action == 'seed':
    root.mkdir(mode=0o750)
    (root / 'file').write_bytes(b'original filesystem state\n')
    os.chown(root / 'file', 1000, 1000)
    os.chmod(root / 'file', 0o640)
    os.utime(root / 'file', ns=(1700000000123456789, 1700000000987654321))
    for key, value in attrs.items(): os.setxattr(root / 'file', key, value)
    os.link(root / 'file', root / 'hardlink')
    (root / 'symlink').symlink_to('file')
    os.mkfifo(root / 'fifo', 0o600)
    with (root / 'sparse').open('wb') as f:
        f.write(b'begin'); f.seek(128 * 1024**2); f.write(b'end')
    shutil.copy2('/bin/true', root / 'capable')
    subprocess.run(['setcap', 'cap_net_bind_service=ep', str(root / 'capable')], check=True)
    subprocess.run(['setfacl', '-m', 'u:1234:r', str(root / 'file')], check=True)
    assert Path('/etc/issue').is_file(), 'whiteout probe requires a file from the base image'
    Path('/etc/issue').unlink()
    shutil.rmtree('/usr/share/doc/adduser', ignore_errors=True)
    Path('/usr/share/doc/adduser').mkdir()
    Path('/usr/share/doc/adduser/only-saved').write_text('opaque directory\n')
    Path('/var/lib/docker/fs-snapshot-marker').write_text('docker mount saved\n')
    Path('/var/lib/containerd/fs-snapshot-marker').write_text('containerd mount saved\n')
    meta = {'mtime_ns': (root / 'file').stat().st_mtime_ns,
            'xattrs': {key: os.getxattr(root / 'file', key).hex() for key in os.listxattr(root / 'file')},
            'capability': os.getxattr(root / 'capable', 'security.capability').hex()}
    (root / 'expected.json').write_text(json.dumps(meta))
elif a.action == 'mutate':
    (root / 'file').write_text('changed after save')
    Path('/var/lib/docker/fs-snapshot-marker').write_text('changed after save')
else:
    meta = json.loads((root / 'expected.json').read_text())
    assert (root / 'file').read_bytes() == b'original filesystem state\n'
    stat = (root / 'file').stat()
    assert (stat.st_uid, stat.st_gid, stat.st_mode & 0o7777) == (1000, 1000, 0o640)
    assert stat.st_mtime_ns == meta['mtime_ns'], (stat.st_mtime_ns, meta)
    assert stat.st_ino == (root / 'hardlink').stat().st_ino
    assert (root / 'symlink').readlink() == Path('file')
    assert {key: os.getxattr(root / 'file', key).hex() for key in os.listxattr(root / 'file')} == meta['xattrs']
    assert os.getxattr(root / 'capable', 'security.capability').hex() == meta['capability']
    assert (root / 'fifo').is_fifo()
    with (root / 'sparse').open('rb') as f:
        assert f.read(5) == b'begin'; f.seek(128 * 1024**2 - 2); assert f.read() == b'\0\0end'
    assert not Path('/etc/issue').exists()
    assert [x.name for x in Path('/usr/share/doc/adduser').iterdir()] == ['only-saved']
    assert Path('/var/lib/docker/fs-snapshot-marker').read_text() == 'docker mount saved\n'
    assert Path('/var/lib/containerd/fs-snapshot-marker').read_text() == 'containerd mount saved\n'
print(json.dumps({'action': a.action, 'passed': True}))

"""Real guest export across host storage layouts; no existing worker is used."""
import json
import errno
import os
from pathlib import Path
import shutil
import subprocess
import struct
import sys
import tarfile
import time

import pytest

from sandweave.sandbox import workspace

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_TRANSFER_ASSETS'), reason='explicit build-transfer assets required')]


@pytest.fixture(scope='module')
def runtime(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('SANDWEAVE_HOME', str(tmp_path_factory.mktemp('transfer-worker')))
        root = workspace.prepare(source=os.environ['SANDWEAVE_TRANSFER_ASSETS'])
    local = Path((root / 'runs/local-path.txt').read_text().strip())
    try:
        yield root
    finally:
        # Every foreground launcher below has completed before fixture teardown.
        shutil.rmtree(local)


GUEST = r'''
import os, pathlib, runpy, sys
print('installation log stays separate', file=sys.stderr, flush=True)
root = pathlib.Path('/tmp/export-fixture')
root.mkdir()
(root / 'workspace').mkdir()
path = root / 'workspace/data'
path.write_bytes(bytes(range(256)) * 4096)
os.chown(path, 1234, 5678)
os.chmod(path, 0o6751)
os.setxattr(path, 'user.sandweave', b'guest metadata')
for name in ('proc', 'sys', 'dev', 'run', 'tmp'):
    (root / name).mkdir()
helpers = pathlib.Path('/tmp/export-helpers')
(helpers / 'fast-io').mkdir(parents=True)
(helpers / 'fast-io/bridge').write_bytes(b'helper executable')
(helpers / 'fast-io/bridge').chmod(0o755)
module = runpy.run_path('/usr/local/bin/engine-build-artifacts', run_name='export-fixture')
module['export'](root, helpers, sys.stdout.buffer)
sys.exit(int(sys.argv[1]))
'''


@pytest.mark.parametrize('layout', ['private', 'shared-group', 'default-acl'])
@pytest.mark.parametrize('exit_code', [0, 2])
def test_guest_export_uses_host_identity(runtime, tmp_path, layout, exit_code):
    parent = tmp_path / 'storage with spaces'
    parent.mkdir(mode=0o700)
    if layout == 'private':
        # Explicitly construct both layouts even on a shared pytest base path.
        os.chown(parent, -1, os.getgid())
        parent.chmod(0o700)
    else:
        groups = [group for group in os.getgroups() if group != os.getgid()]
        if not groups:
            pytest.skip('host has no supplementary group')
        os.chown(parent, -1, groups[0])
        parent.chmod(0o2750)
        if layout == 'default-acl':
            acl = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', tag, permissions, 0xffffffff)
                for tag, permissions in ((1, 7), (4, 5), (32, 0)))
            try:
                os.setxattr(parent, 'system.posix_acl_default', acl)
            except OSError as error:
                if error.errno == errno.EOPNOTSUPP:
                    pytest.skip('this filesystem does not support POSIX default ACLs')
                raise
    before = parent.stat()
    output = parent / 'artifacts'
    name = 'transfer-' + layout + '-' + str(exit_code)
    log = parent / 'launcher.log'
    command = [sys.executable, str(runtime / 'scripts/run-gvisor.py'), '--guest-gs',
               '--no-runtime-debug', '--network-policy', 'offline', '--cpu-policy', 'shared',
               '--guest-cpus', '1', '--memory-mib', '256', '--build-output', str(output),
               name, '--', 'python3', '-c', GUEST, str(exit_code)]
    with log.open('wb') as logs:
        process = subprocess.Popen(command, stdout=logs, stderr=subprocess.STDOUT)
        try:
            result = process.wait(timeout=60)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
    assert result == exit_code, log.read_text(errors='replace')
    assert 'installation log stays separate' in log.read_text()
    assert not list(parent.glob('.build-output-*'))
    after = parent.stat()
    assert (before.st_mode, before.st_uid, before.st_gid) == (after.st_mode, after.st_uid, after.st_gid)
    spec = json.loads((Path((runtime / 'runs/local-path.txt').read_text().strip()) /
                       'gvisor/bundles' / name / 'config.json').read_text())
    assert all(mount['destination'] != '/sandweave-output' for mount in spec['mounts'])
    if exit_code:
        assert not output.exists()  # Even a complete stream needs a successful guest.
        return
    archive = output / 'rootfs.tar'
    with tarfile.open(archive) as image:
        entry = image.getmember('./workspace/data')
        assert (entry.uid, entry.gid, entry.mode) == (1234, 5678, 0o6751)
        assert entry.pax_headers['SCHILY.xattr.user.sandweave'] == 'guest metadata'
        assert image.extractfile(entry).read() == bytes(range(256)) * 4096
    for path in (archive, output / 'fast-io/bridge'):
        info = path.stat()
        assert info.st_uid == os.getuid() and info.st_gid == before.st_gid
        assert not info.st_mode & 0o6000
    assert (output / 'fast-io/bridge').read_bytes() == b'helper executable'


def test_interrupted_transfer_leaves_no_published_output(runtime, tmp_path):
    output = tmp_path / 'interrupted-output'
    name = 'transfer-interrupted'
    guest = ("import os,struct,time; os.write(1,b'SANDWEAVE-BUILD-1\\n'+"
             "struct.pack('!I',7)+b'partial'); time.sleep(60)")
    command = [sys.executable, str(runtime / 'scripts/run-gvisor.py'), '--guest-gs',
               '--no-runtime-debug', '--network-policy', 'offline', '--cpu-policy', 'shared',
               '--guest-cpus', '1', '--memory-mib', '256', '--build-output', str(output),
               name, '--', 'python3', '-c', guest]
    with (tmp_path / 'interrupted.log').open('wb') as logs:
        process = subprocess.Popen(command, stdout=logs, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while not list(tmp_path.glob('.build-output-*/rootfs.tar.part')):
                assert process.poll() is None and time.monotonic() < deadline
                time.sleep(.05)
            process.terminate()
            assert process.wait(timeout=15) != 0
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
    assert not output.exists() and not list(tmp_path.glob('.build-output-*'))
    owned = json.loads((runtime / 'runs/gvisor' / name / 'owned-processes.json').read_text())
    for child in owned['processes']:
        status = Path('/proc') / str(child['pid']) / 'stat'
        if status.exists():
            assert status.read_text().rsplit(')', 1)[1].split()[0] == 'Z'

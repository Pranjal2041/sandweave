"""Guest metadata must not become a host ownership requirement."""
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import struct
import sys
import tarfile

import pytest


@pytest.fixture
def artifacts():
    path = Path(__file__).parents[1] / 'scripts/build_artifacts.py'
    spec = importlib.util.spec_from_file_location('build_artifacts', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def archive(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as result:
        for name, data, kind in entries:
            member = tarfile.TarInfo(name)
            member.uid, member.gid, member.mode = 1234567, 7654321, 0o6755
            member.pax_headers = {'SCHILY.xattr.user.example': 'guest metadata'}
            if kind == 'file':
                member.size = len(data)
                result.addfile(member, io.BytesIO(data))
            else:
                member.type = {'symlink': tarfile.SYMTYPE, 'hardlink': tarfile.LNKTYPE,
                               'directory': tarfile.DIRTYPE, 'fifo': tarfile.FIFOTYPE}[kind]
                member.linkname = data
                result.addfile(member)
    return output.getvalue()


def wire(artifacts, rootfs, helpers):
    output = bytearray(artifacts.MAGIC)
    for data in (rootfs, helpers):
        for start in range(0, len(data), artifacts.CHUNK):
            chunk = data[start:start + artifacts.CHUNK]
            output.extend(struct.pack('!I', len(chunk)))
            output.extend(chunk)
        output.extend(struct.pack('!I', 0) + hashlib.sha256(data).digest())
    return bytes(output) + artifacts.COMPLETE


@pytest.mark.parametrize('shared_group', [False, True])
def test_host_writes_preserve_guest_metadata_only_in_image(artifacts, tmp_path, shared_group):
    output = tmp_path / 'host output with spaces'
    output.mkdir()
    if shared_group:
        groups = [group for group in os.getgroups() if group != os.getgid()]
        if not groups:
            pytest.skip('host has no supplementary group')
        os.chown(output, -1, groups[0])
        output.chmod(0o2750)
    before = output.stat()
    payload = bytes(range(256)) * 5000
    rootfs = archive([('./workspace/data', payload, 'file')])
    helpers = archive([
        ('./fast-io/bridge', payload, 'file'),
        ('./gpu/libactual.so', b'library', 'file'),
        ('./gpu/libalias.so', 'libactual.so', 'symlink'),
        ('./gpu/libcopy.so', './gpu/libactual.so', 'hardlink'),
        ('./gpu/guest-path', '/opt/guest-only', 'symlink')])
    artifacts.receive(io.BytesIO(wire(artifacts, rootfs, helpers)), output)
    artifacts.extract_helpers(output / 'helpers.tar', output)
    assert (output / 'rootfs.tar').read_bytes() == rootfs
    with tarfile.open(output / 'rootfs.tar') as image:
        member = image.getmember('./workspace/data')
        assert (member.uid, member.gid, member.mode) == (1234567, 7654321, 0o6755)
        assert member.pax_headers['SCHILY.xattr.user.example'] == 'guest metadata'
    for name in ('rootfs.tar', 'fast-io/bridge', 'gpu/libactual.so', 'gpu/libcopy.so'):
        info = (output / name).stat()
        assert info.st_uid == os.getuid()
        assert info.st_gid == before.st_gid
        assert not info.st_mode & 0o6000
    assert (output / 'fast-io/bridge').read_bytes() == payload
    assert (output / 'fast-io/bridge').stat().st_mode & 0o777 == 0o755
    assert (output / 'gpu/libalias.so').read_bytes() == b'library'
    assert (output / 'gpu/libcopy.so').read_bytes() == b'library'
    assert (output / 'gpu/guest-path').readlink() == Path('/opt/guest-only')
    after = output.stat()
    assert (after.st_mode, after.st_uid, after.st_gid) == (before.st_mode, before.st_uid, before.st_gid)


@pytest.mark.parametrize('failure', ['short-header', 'short-data', 'short-checksum',
                                    'short-finish', 'checksum', 'oversized', 'trailing'])
def test_incomplete_or_corrupt_transfer_is_never_accepted(artifacts, tmp_path, failure):
    data = wire(artifacts, b'root filesystem', b'helpers')
    if failure == 'short-header':
        data = data[:4]
    elif failure == 'short-data':
        data = data[:len(artifacts.MAGIC) + 7]
    elif failure == 'short-checksum':
        data = data[:len(artifacts.MAGIC) + 4 + len(b'root filesystem') + 4 + 3]
    elif failure == 'short-finish':
        data = data[:-1]
    elif failure == 'checksum':
        index = len(artifacts.MAGIC) + 4
        data = data[:index] + b'X' + data[index + 1:]
    elif failure == 'oversized':
        data = artifacts.MAGIC + struct.pack('!I', artifacts.CHUNK + 1)
    else:
        data += b'unexpected bytes'
    with pytest.raises(ValueError):
        artifacts.receive(io.BytesIO(data), tmp_path)


def test_guest_export_streams_real_archives_and_excludes_build_helpers(artifacts, tmp_path):
    root = tmp_path / 'guest'
    helpers = root / 'sandweave-output'
    (helpers / 'fast-io').mkdir(parents=True)
    (helpers / 'fast-io/bridge').write_bytes(b'helper executable')
    (root / 'sandweave-input').mkdir()
    (root / 'sandweave-input/source').write_text('build input')
    (root / 'workspace').mkdir()
    (root / 'workspace/data').write_bytes(bytes(range(256)) * 3000)
    for name in ('proc', 'sys', 'dev', 'run', 'tmp'):
        (root / name).mkdir()
    output = io.BytesIO()
    artifacts.export(root, helpers, output)
    received = tmp_path / 'received'
    received.mkdir()
    artifacts.receive(io.BytesIO(output.getvalue()), received)
    with tarfile.open(received / 'rootfs.tar') as image:
        assert image.extractfile('./workspace/data').read() == (root / 'workspace/data').read_bytes()
        assert not any(name.startswith(('./sandweave-input', './sandweave-output')) for name in image.getnames())
        assert all(image.getmember('./' + name).isdir() for name in ('proc', 'sys', 'dev', 'run', 'tmp'))
    artifacts.extract_helpers(received / 'helpers.tar', received)
    assert (received / 'fast-io/bridge').read_bytes() == b'helper executable'


def test_failed_producer_cannot_send_a_success_checksum(artifacts):
    output = io.BytesIO()
    with pytest.raises(ValueError, match='archive creation failed'):
        artifacts.send_archive(output, [sys.executable, '-c',
            "import sys; sys.stdout.buffer.write(b'partial'); sys.exit(2)"])
    assert output.getvalue() == struct.pack('!I', 7) + b'partial'


@pytest.mark.parametrize('entries', [
    [('../outside', b'bad', 'file')],
    [('/outside', b'bad', 'file')],
    [('gpu/../../outside', b'bad', 'file')],
    [('gpu/escape', '../../outside', 'symlink'), ('gpu/escape/file', b'bad', 'file')],
    [('gpu/link', '../outside', 'hardlink')],
    [('gpu/fifo', '', 'fifo')],
    [('gpu/file', b'first', 'file'), ('gpu/file', b'second', 'file')],
])
def test_helpers_cannot_write_outside_their_directory(artifacts, tmp_path, entries):
    source = tmp_path / 'helpers.tar'
    source.write_bytes(archive(entries))
    output = tmp_path / 'output'
    output.mkdir()
    with pytest.raises((ValueError, OSError)):
        artifacts.extract_helpers(source, output)
    assert not (tmp_path / 'outside').exists()

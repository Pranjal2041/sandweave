import hashlib
import io
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import tarfile
import zipfile
from types import SimpleNamespace

import pytest

from sandweave.templates.disk_image import acl, extract, validate, write_filesystem


def test_archive_member_is_streamed_without_writing_its_path(tmp_path):
    archive, output = tmp_path / 'image.zip', tmp_path / 'image.qcow2'
    data = b'qcow test data'
    with zipfile.ZipFile(archive, 'w') as source:
        source.writestr('../../escape/image.qcow2', data)
    extract(archive, {'image_sha256': hashlib.sha256(data).hexdigest()}, output)
    assert output.read_bytes() == data
    assert not (tmp_path.parent / 'escape').exists()
    output.unlink()
    with pytest.raises(ValueError, match='checksum'):
        extract(archive, {'image_sha256': '0' * 64}, output)
    assert not output.exists()


def test_ext4_compact_acl_converts_named_and_unnamed_entries():
    source = struct.pack('<IHHHHIHHHHHH', 1, 1, 7, 2, 4, 1000, 4, 5, 16, 5, 32, 0)
    expected = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *entry) for entry in
        [(1, 7, 0xffffffff), (2, 4, 1000), (4, 5, 0xffffffff), (16, 5, 0xffffffff), (32, 0, 0xffffffff)])
    assert acl(source) == expected


def test_export_preserves_linux_files_and_xattrs(tmp_path):
    pytest.importorskip('dissect.extfs')
    from dissect.extfs import ExtFS
    if not shutil.which('mkfs.ext4') or not shutil.which('debugfs'):
        pytest.skip('e2fsprogs is required for an independent ext4 fixture')
    disk = tmp_path / 'root.ext4'
    with disk.open('wb') as stream:
        stream.truncate(32 * 1024**2)
    subprocess.run(['mkfs.ext4', '-q', '-F', str(disk)], check=True)
    content = tmp_path / 'payload'
    content.write_bytes(b'original bytes\0\xff\n')
    commands = tmp_path / 'debugfs.commands'
    commands.write_text(f'''mkdir /etc
write {content} /etc/payload
set_inode_field /etc/payload mode 0104751
set_inode_field /etc/payload uid 1000
set_inode_field /etc/payload gid 1001
ea_set /etc/payload user.example preserved
ea_set /etc/payload user.large {'x' * 500}
symlink /etc/link payload
ln /etc/payload /etc/hardlink
set_inode_field /etc/payload links_count 2
''')
    subprocess.run(['debugfs', '-w', '-f', str(commands), str(disk)], check=True, capture_output=True)
    output = tmp_path / 'export.tar'
    with disk.open('rb') as stream:
        write_filesystem(ExtFS(stream), output)
    with tarfile.open(output) as archive:
        data = archive.getmember('etc/hardlink')
        assert archive.extractfile('etc/payload').read() == content.read_bytes()
        assert archive.extractfile(data).read() == content.read_bytes()
        assert data.uid == 1000 and data.gid == 1001 and data.mode == 0o4751
        assert data.pax_headers['SCHILY.xattr.user.example'] == 'preserved'
        assert archive.getmember('etc/link').linkname == 'payload'
        assert archive.getmember('etc/payload').islnk()
    # Zero inode-body xattr magic with nonzero slack is legal. External xattrs
    # must still be read; clearing all attributes would lose that metadata.
    with disk.open('rb') as stream:
        node = ExtFS(stream).get('/etc/payload')
        assert node.inode.i_file_acl_lo
        node.inode.i_extra = b'\0' * 4 + b'unused inode slack'
        write_filesystem(SimpleNamespace(root=node), output)
    with tarfile.open(output) as archive:
        assert archive.extractfile('.').read() == content.read_bytes()
        assert archive.getmember('.').pax_headers['SCHILY.xattr.user.large'] == 'x' * 500

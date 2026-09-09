import io
import os
from pathlib import Path
import stat
import tarfile
from types import SimpleNamespace
import zipfile

import pytest

from sandweave.bootstrap import extract_source
from sandweave.vr_installation import extract_zip
from sandweave import installer_tools


def cpio_entry(name, data=b'', *, mode=stat.S_IFREG | 0o644, inode=1, links=1):
    name = os.fsencode(name) + b'\0'
    fields = (inode, mode, 0, 0, links, 0, len(data), 0, 0, 0, 0, len(name), 0)
    header = b'070701' + ''.join(f'{value:08x}' for value in fields).encode() + name
    return header + b'\0' * (-len(header) % 4) + data + b'\0' * (-len(data) % 4)


def test_rpm_payload_extraction_preserves_files_and_links(tmp_path, monkeypatch):
    payload = b''.join([
        cpio_entry('.', mode=stat.S_IFDIR | 0o755),
        cpio_entry('usr/bin', mode=stat.S_IFDIR | 0o755),
        cpio_entry('usr/bin/first', inode=17, links=2, mode=stat.S_IFREG | 0o755),
        cpio_entry('usr/bin/second', b'complete executable', inode=17, links=2,
                   mode=stat.S_IFREG | 0o755),
        cpio_entry('usr/bin/alias', b'first', mode=stat.S_IFLNK | 0o777),
        cpio_entry('TRAILER!!!'), b'\0' * 512,
    ])
    stream = io.BytesIO(payload)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(installer_tools.sys, 'stdin', SimpleNamespace(buffer=stream))
    installer_tools.cpio(['-idum'])
    first, second = tmp_path / 'usr/bin/first', tmp_path / 'usr/bin/second'
    assert first.read_bytes() == b'complete executable'
    assert first.samefile(second)
    assert first.stat().st_mode & 0o111
    assert (tmp_path / 'usr/bin/alias').readlink() == Path('first')
    assert stream.tell() == len(payload)


def test_cpio_rejects_parent_symlink_escape(tmp_path, monkeypatch):
    root, outside = tmp_path / 'root', tmp_path / 'outside'
    root.mkdir(); outside.mkdir()
    payload = cpio_entry('escape', os.fsencode(outside), mode=stat.S_IFLNK | 0o777)
    payload += cpio_entry('escape/user-file', b'bad') + cpio_entry('TRAILER!!!')
    monkeypatch.chdir(root)
    monkeypatch.setattr(installer_tools.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(payload)))
    with pytest.raises(ValueError, match='outside'):
        installer_tools.cpio(['-idum'])
    assert not (outside / 'user-file').exists()


def test_source_tar_preserves_executable_without_host_ownership(tmp_path):
    path = tmp_path / 'source.tar'
    with tarfile.open(path, 'w') as archive:
        entry = tarfile.TarInfo('revision/build.sh')
        data = b'#!/bin/sh\nexit 0\n'
        entry.size, entry.mode, entry.uid = len(data), 0o755, 123456
        archive.addfile(entry, io.BytesIO(data))
        link = tarfile.TarInfo('revision/alias')
        link.type, link.linkname = tarfile.SYMTYPE, 'build.sh'
        archive.addfile(link)
    extract_source(path, tmp_path / 'output')
    script = tmp_path / 'output/build.sh'
    assert script.read_bytes() == data and script.stat().st_uid == os.getuid()
    assert script.stat().st_mode & 0o111
    assert (tmp_path / 'output/alias').read_bytes() == data


@pytest.mark.parametrize('name', ['/absolute', '../outside', 'revision/../../outside'])
def test_source_archives_reject_escaping_paths(tmp_path, name):
    path = tmp_path / 'source.tar'
    with tarfile.open(path, 'w') as archive:
        archive.addfile(tarfile.TarInfo(name))
    with pytest.raises(ValueError):
        extract_source(path, tmp_path / 'output')
    zipped = tmp_path / 'source.zip'
    with zipfile.ZipFile(zipped, 'w') as archive:
        archive.writestr(name, b'bad')
    with pytest.raises(ValueError):
        extract_zip(zipped, tmp_path / 'zip-output')


def test_short_package_is_an_error():
    with pytest.raises(ValueError, match='Truncated'):
        installer_tools.exact(io.BytesIO(b'partial'), 20)


def test_zip_does_not_follow_an_existing_external_link(tmp_path):
    outside, output = tmp_path / 'outside', tmp_path / 'output'
    outside.mkdir()
    output.mkdir()
    (output / 'escape').symlink_to(outside, target_is_directory=True)
    archive = tmp_path / 'source.zip'
    with zipfile.ZipFile(archive, 'w') as stream:
        stream.writestr('escape/user-file', b'bad')
    with pytest.raises(ValueError, match='outside'):
        extract_zip(archive, output)
    assert not (outside / 'user-file').exists()

"""Owned-volume import cannot turn guest paths into arbitrary host reads."""
import os
from pathlib import Path

import pytest

from sandweave.sandbox.image_import import volume_file


def test_volume_archive_opens_only_regular_files_beneath_owned_storage(tmp_path):
    root = tmp_path / 'volume'
    (root / 'data/subdir').mkdir(parents=True)
    record = {'service_volumes': {'build': str(root)}}
    archive = root / 'data/subdir/image.tar'
    archive.write_bytes(b'archive')
    with volume_file(record, 'build', 'subdir/image.tar') as stream:
        assert stream.read() == b'archive'
    outside = tmp_path / 'outside'
    outside.write_bytes(b'private host file')
    (root / 'data/link').symlink_to(outside)
    (root / 'data/parent').symlink_to(tmp_path, target_is_directory=True)
    os.mkfifo(root / 'data/pipe')
    before = len(list(Path('/proc/self/fd').iterdir()))
    for path in ('../outside', str(outside), 'link', 'parent/outside', 'pipe', 'subdir', '.', ''):
        with pytest.raises((OSError, ValueError)):
            volume_file(record, 'build', path)
    with pytest.raises(KeyError):
        volume_file(record, 'another-sandbox', 'subdir/image.tar')
    assert len(list(Path('/proc/self/fd').iterdir())) == before

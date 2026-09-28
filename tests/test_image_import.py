"""Owned-volume import cannot turn guest paths into arbitrary host reads."""
import os
from pathlib import Path
import hashlib
from types import SimpleNamespace

import pytest

from sandweave.sandbox.image_import import volume_file, upload


def test_upload_retries_checksums_and_private_namespaces(tmp_path, monkeypatch):
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path/'home'))
    worker = SimpleNamespace(root=tmp_path, read=lambda identity: {
        'state': 'ready', 'spec': {'_image_import_memory': 384*1024**2}})
    payload = b'OCI bytes' * 100
    upload(worker, 'one', 'write', data=payload[:20])
    upload(worker, 'two', 'write', data=b'unrelated')
    upload(worker, 'one', 'write', data=payload[:20])
    with pytest.raises(ValueError, match='gap'):
        upload(worker, 'one', 'write', offset=21, data=b'gap')
    upload(worker, 'one', 'write', offset=20, data=payload[20:])
    with pytest.raises(ValueError, match='checksum'):
        upload(worker, 'one', 'finish', offset=len(payload), sha256='a'*64)
    digest = hashlib.sha256(payload).hexdigest()
    result = upload(worker, 'one', 'finish', offset=len(payload), sha256=digest)
    assert result == {'format': 'oci', 'sha256': digest}
    assert upload(worker, 'one', 'finish', offset=len(payload), sha256=digest) == result
    with pytest.raises(ValueError, match='immutable'):
        upload(worker, 'one', 'write', data=b'changed')
    assert (tmp_path/'home/images/archives'/(digest+'.tar')).read_bytes() == payload
    assert (tmp_path/'image-uploads/two/archive.tar').read_bytes() == b'unrelated'


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

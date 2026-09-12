"""Count actual checksum reads through publication and independent attachment."""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from sandweave.sandbox.artifacts import Artifacts
from sandweave.sandbox.errors import IncompatibleSnapshot
from sandweave.sandbox.snapshots import Store
from sandweave.sandbox import workspace


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(workspace.engine_sources()))
    import runtime_store
    root = tmp_path / 'source'
    (root / 'images').mkdir(parents=True)
    (root / 'tools').mkdir()
    (root / 'images/base').write_bytes(b'immutable image\n' * 65536)
    binary = root / 'binary'
    binary.write_bytes(b'runtime')
    runtime = runtime_store.publish(root, {'runsc': binary})
    snapshot = root / 'snapshot'
    snapshot.mkdir()
    files = {'lab-spec.json': b'{}', 'launch-settings.json': json.dumps({'runtime': runtime}).encode(),
             'fixtures.tar': b'fixtures', 'rootfs-upper.tar': b'filesystem'}
    for name, payload in files.items():
        (snapshot / name).write_bytes(payload)
    image = root / 'images/base'
    manifest = {'format': 2, 'snapshot_id': 'test-baseline', 'kind': 'filesystem',
                'filesystem': {'mounts': []}, 'runtime': runtime,
                'base_image': {'path': 'images/base', 'size': image.stat().st_size,
                               'sha256': hashlib.sha256(image.read_bytes()).hexdigest()},
                'files': {name: {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                          for name, data in files.items()}}
    (snapshot / 'snapshot-manifest.json').write_text(json.dumps(manifest))
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'source-store'))
    store = Store(SimpleNamespace(root=root))
    worker = SimpleNamespace(root=root, store=store)
    artifacts = Artifacts(worker)
    metadata = dict(id='snap-' + 'a' * 32, spec={'runtime': 'gvisor'}, workspace=str(root),
                    location=str(snapshot), digest='test-metadata')
    artifacts._publish(metadata)
    return worker, metadata


def test_publication_hashes_base_once_and_new_worker_attaches_without_reread(saved, tmp_path, monkeypatch):
    worker, metadata = saved
    reads = []
    original = hashlib.file_digest
    def counting(stream, algorithm, **options):
        if Path(stream.name).name == 'base':
            reads.append(str(stream.name))
        return original(stream, algorithm, **options)
    monkeypatch.setattr(hashlib, 'file_digest', counting)
    publisher = Artifacts(worker, shared_cache=tmp_path / 'shared')
    assert publisher.cache(metadata['id'])['ready']
    assert reads == [str(worker.root / 'images/base')], reads
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'second-store'))
    other = SimpleNamespace(root=tmp_path / 'second')
    other.store = Store(SimpleNamespace(root=other.root))
    linked = publisher.directory(metadata['id']) / 'workspace/images/base'
    os.link(linked, tmp_path / 'another-worker-image')
    monkeypatch.setattr(other.store, 'verify', lambda *a, **kw: pytest.fail('completed publication was verified again'))
    assert Artifacts(other, shared_cache=tmp_path / 'shared').cached(metadata['id'])['ready']
    assert len(reads) == 1, reads
    print(json.dumps({'base_checksum_passes': len(reads), 'publication_and_attachments': 2,
                      'base_bytes': (worker.root / 'images/base').stat().st_size}))


def test_transfer_checks_new_bytes_once_and_rejects_same_size_corruption(saved, tmp_path, monkeypatch):
    worker, metadata = saved
    source = Artifacts(worker)
    manifest = source.manifest(metadata['id'])
    other = SimpleNamespace(root=tmp_path / 'destination')
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'destination-store'))
    other.store = Store(SimpleNamespace(root=other.root))
    target = Artifacts(other)
    for name in target.begin(manifest):
        size = manifest['files'][name]['size']
        for offset in range(0, size, 1024**2):
            target.write(metadata['id'], name, offset, source.read(metadata['id'], name, offset, min(1024**2, size-offset)))
    image = target.directory(metadata['id']) / 'workspace/images/base'
    image.write_bytes(b'x' * image.stat().st_size)
    with pytest.raises((IncompatibleSnapshot, workspace.ResourceUnavailable), match='checksum'):
        target.finish(metadata['id'])
    assert not (target.directory(metadata['id']) / 'complete').exists()


def test_changed_source_does_not_reuse_a_verification_receipt(saved):
    worker, metadata = saved
    assert worker.store.verify(metadata, reuse=True)['status'] == 'passed'
    image = worker.root / 'images/base'
    old = image.stat()
    image.write_bytes(b'x' * old.st_size)
    os.utime(image, ns=(old.st_atime_ns, old.st_mtime_ns + 1000000000))
    assert worker.store.verify(metadata, reuse=True)['status'] == 'failed'


def test_copy_cannot_stamp_changed_source_with_the_previous_checksum(tmp_path, monkeypatch):
    import errno
    source, destination = tmp_path / 'source', tmp_path / 'copy'
    source.write_bytes(b'original')
    expected = hashlib.sha256(b'original').hexdigest()
    verified = {}
    workspace.verified_digest(source, sha256=expected, verified=verified)
    original_copy = workspace.shutil.copy2
    def copy_then_change(src, dst):
        original_copy(src, dst)
        stamp = source.stat()
        source.write_bytes(b'modified')
        os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1000000000))
    def different_filesystem(*args):
        raise OSError(errno.EXDEV, 'cross-device copy')
    monkeypatch.setattr(workspace.os, 'link', different_filesystem)
    monkeypatch.setattr(workspace.shutil, 'copy2', copy_then_change)
    workspace._immutable(source, destination, sha256=expected, verified=verified)
    assert destination.read_bytes() == b'original'
    assert str(source) not in verified
    with pytest.raises(workspace.ResourceUnavailable, match='checksum'):
        workspace.verified_digest(source, sha256=expected, verified=verified)


def test_native_snapshot_import_tracks_upper_paths_and_guest_symlinks(tmp_path, monkeypatch):
    from sandweave.sandbox.runtimes.apptainer.driver import inventory, verify
    monkeypatch.syspath_prepend(str(workspace.engine_sources()))
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'store'))
    source = tmp_path / 'source'
    saved = source / 'snapshots/saved'
    upper = saved / 'upper'
    (upper / 'opt/example').mkdir(parents=True)
    (upper / 'opt/example/value').write_text('saved')
    (upper / 'guest-link').symlink_to('/guest-only-path')
    image = source / 'base.sif'
    image.write_bytes(b'native image fixture')
    manifest = {'backend': 'apptainer', 'kind': 'filesystem', 'snapshot_id': 'native-test',
                'base_image': {'path': 'base.sif', 'size': image.stat().st_size,
                               'sha256': hashlib.sha256(image.read_bytes()).hexdigest()},
                'files': inventory(upper)}
    (saved / 'snapshot-manifest.json').write_text(json.dumps(manifest))
    assert verify(source, saved)['status'] == 'passed'
    destination = tmp_path / 'destination'
    store = Store(SimpleNamespace(root=destination))
    record = {'id': 'snap-' + 'b' * 32, 'workspace': str(source), 'location': str(saved),
              'spec': {'runtime': 'apptainer'}}
    imported = store.materialize(record)
    assert (imported / 'upper/opt/example/value').read_text() == 'saved'
    assert (imported / 'upper/guest-link').readlink() == Path('/guest-only-path')
    assert store.materialize(record) == imported
    (imported / 'upper/guest-link').unlink()
    (imported / 'upper/guest-link').symlink_to('/changed')
    with pytest.raises(IncompatibleSnapshot):
        store.materialize(record)

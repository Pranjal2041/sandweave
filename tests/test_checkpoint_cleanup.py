import json
from pathlib import Path
import shutil
import socket
import threading

import pytest
from sandweave.sandbox import workspace


@pytest.fixture
def capture(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(workspace.engine_sources()))
    import runtime_store, snapshot_store
    lab = tmp_path / 'lab'; local = tmp_path / 'local'
    (lab / 'runs').mkdir(parents=True); (lab / 'tools').mkdir()
    (lab / 'runs/local-path.txt').write_text(str(local))
    binary = lab / 'binary'; binary.write_bytes(b'engine')
    runtime = runtime_store.publish(lab, {'runsc': binary})
    (lab / 'images').mkdir(); (lab / 'images/base').write_bytes(b'base')
    source = local / 'gvisor/checkpoints/capture'; source.mkdir(parents=True)
    payloads = {'checkpoint.img': b'kernel', 'pages.img': b'pages', 'fixtures.tar': b'fixture',
                'lab-spec.json': b'{}', 'launch-settings.json': json.dumps({'runtime': runtime}).encode()}
    for name, value in payloads.items(): (source / name).write_bytes(value)
    manifest = {'format': 2, 'snapshot_id': 'capture', 'runtime': runtime, 'discard_local_staging': True,
        'base_image': {'path': 'images/base', 'size': 4},
        'files': {name: {'size': len(value)} for name, value in payloads.items()},
        'verification_source': {'hostname': socket.gethostname(), 'path': str(source),
            'files': {name: snapshot_store.signature(source / name) for name in payloads},
            'base_path': str(lab / 'images/base'), 'base_stat': snapshot_store.signature(lab / 'images/base')}}
    snapshot_store.write_json(source / 'snapshot-manifest.json', manifest)
    saved = lab / 'snapshots/capture'; shutil.copytree(source, saved)
    snapshot_store.pending(saved, manifest)
    return snapshot_store, runtime_store, lab, local, source, saved, manifest


def test_publication_cleanup_does_not_wait_for_or_break_restores(capture, monkeypatch):
    store, runtime, lab, local, source, saved, manifest = capture
    started, proceed = threading.Event(), threading.Event()
    digest = runtime.digest
    def slow(path):
        if Path(path) == source / 'pages.img':
            started.set(); assert proceed.wait(5)
        return digest(path)
    monkeypatch.setattr(runtime, 'digest', slow)
    results = []
    verifier = threading.Thread(target=lambda: results.append(store.verify(lab, saved)))
    verifier.start()
    try:
        assert started.wait(5)
        assert source.exists()
        # A pending restore chooses durable files without a checksum scan or lock.
        assert store.restore_path(local, saved, manifest) == saved
        with (saved / 'pages.img').open('rb') as reader:
            proceed.set(); verifier.join(5)
            assert not verifier.is_alive()
            assert results[0]['status'] == 'passed'
            assert not source.exists()
            assert reader.read() == b'pages'
    finally:
        proceed.set(); verifier.join(5)
    assert store.verify(lab, saved)['status'] == 'passed'


def test_corrupt_publication_retains_recoverable_source(capture):
    store, _, lab, _, source, saved, _ = capture
    (saved / 'pages.img').write_bytes(b'WRONG')
    assert store.verify(lab, saved)['status'] == 'failed'
    assert (source / 'pages.img').read_bytes() == b'pages'
    shutil.copy2(source / 'pages.img', saved / 'pages.img')
    assert store.verify(lab, saved)['status'] == 'passed'
    assert not source.exists()


def test_cleanup_failure_can_retry_without_invalidating_snapshot(capture, monkeypatch):
    store, _, lab, _, source, saved, _ = capture
    original = store.cleanup_staging
    def denied(*args):
        raise PermissionError('temporarily unavailable')
    monkeypatch.setattr(store, 'cleanup_staging', denied)
    result = store.verify(lab, saved)
    assert result['status'] == 'passed' and 'cleanup_error' in result
    assert source.exists()
    monkeypatch.setattr(store, 'cleanup_staging', original)
    assert store.verify(lab, saved)['status'] == 'passed'
    assert not source.exists()


@pytest.mark.parametrize('kind', ['legacy', 'local_only', 'foreign', 'replaced'])
def test_cleanup_keeps_non_disposable_or_unowned_data(capture, kind):
    store, _, lab, _, source, saved, manifest = capture
    if kind == 'legacy': manifest.pop('discard_local_staging')
    elif kind == 'local_only': saved = source
    elif kind == 'foreign': manifest['verification_source']['hostname'] = 'another-node'
    else:
        original = dict(manifest, snapshot_id='someone-else')
        store.write_json(source / 'snapshot-manifest.json', original)
    store.cleanup_staging(lab, saved, manifest)
    assert source.exists()

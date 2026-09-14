from concurrent.futures import ThreadPoolExecutor
import hashlib
import threading
import time

import pytest

from sandweave.sandbox import workspace
from sandweave.sandbox.runtimes.gvisor import engine
from sandweave.templates import disk_image, images
from sandweave.templates.resolve import fingerprint


def test_concurrent_workers_share_one_feature_build(tmp_path, monkeypatch):
    from sandweave import bootstrap
    calls = []
    barrier = threading.Barrier(8)

    class Builder:
        def __init__(self, directory):
            self.downloads = directory / 'downloads'
            self.downloads.mkdir(exist_ok=True)

        def pull(self, source, destination):
            destination.write_bytes(b'builder')

        def engine(self, directory):
            calls.append(directory)
            time.sleep(.05)
            workspace.atomic_json(directory / 'tools/gvisor-socket/runtime.json', {'path': 'built'})

    monkeypatch.setattr(bootstrap, 'Builder', Builder)

    def build(_):
        barrier.wait(timeout=5)
        return engine.build_features(tmp_path)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(build, range(8)))
    assert len(calls) == 1
    assert len(set(results)) == 1
    assert (results[0] / 'tools/gvisor-socket/runtime.json').is_file()
    assert not list((tmp_path / 'assets').glob('.feature-engine-*'))


def test_reusing_disk_image_does_not_rehash_it_for_each_worker_or_pool(tmp_path, monkeypatch):
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'state'))
    image = {'url': 'https://example.org/desktop.qcow2', 'format': 'qcow2', 'sha256': 'a' * 64}
    key = fingerprint({'image': image, 'python': images.PYTHON_SHA256, 'format': 1})
    original = workspace.home() / 'images' / ('disk-' + key + '.erofs')
    original.parent.mkdir(parents=True)
    original.write_bytes(b'prepared filesystem')
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    workspace.atomic_json(original.with_suffix('.json'), {
        'sha256': digest, 'signature': workspace.file_signature(original),
        'partition': 3, 'filesystem_uuid': 'original', 'virtual_size': 123})
    reads = []
    real_digest = workspace.file_digest

    def count(path):
        reads.append(str(path))
        return real_digest(path)

    monkeypatch.setattr(workspace, 'file_digest', count)
    pool = 'pool-' + 'a' * 32
    for index in range(3):
        result = disk_image.prepare(image, tmp_path / ('worker-' + str(index)), pool=pool)
        assert result['digest'] == 'sha256:' + digest
    assert reads == []
    # A content mutation invalidates the receipt, even if the byte count is unchanged.
    pooled = workspace.home() / 'images/pools' / pool / original.name
    pooled.write_bytes(b'corrupt! filesystem')
    from sandweave import bootstrap
    def download(*args, **kwargs):
        raise RuntimeError('rebuild required')
    monkeypatch.setattr(bootstrap, 'download', download)
    with pytest.raises(RuntimeError, match='rebuild required'):
        disk_image.prepare(image, tmp_path / 'worker-4', pool=pool)
    assert str(pooled) in reads

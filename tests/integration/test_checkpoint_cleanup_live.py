"""Published checkpoints remain restorable after automatic staging reclamation."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path

import pytest
from sandweave import Sandbox, Memory
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit disposable worker required')]


@pytest.fixture(scope='module', autouse=True)
def stop_owned_worker():
    yield
    c = local_connection()
    try:
        c.call('_shutdown_if_idle')
    finally:
        c.close()


@pytest.mark.parametrize('state', ['filesystem', 'memory'])
def test_repeated_snapshots_reclaim_staging_while_clones_run(state):
    c = local_connection()
    try:
        root = Path(c.call('ping')['workspace'])
    finally:
        c.close()
    local = Path((root / 'runs/local-path.txt').read_text().strip())
    expected = os.environ.get('SANDWEAVE_LOCAL_DIR') or os.environ.get('TMPDIR')
    if expected:
        assert local.parent == Path(expected).resolve()
    with Sandbox(memory=Memory('256MiB','512MiB')) as source:
        source.run('dd if=/dev/zero of=/workspace/large bs=1M count=32',check=True)
        for index in range(3):
            source.files.write_text('/workspace/iteration',str(index))
            saved = source.snapshot(state=state)
            manifest = json.loads((Path(saved.location)/'snapshot-manifest.json').read_text())
            staging = Path(manifest['verification_source']['path'])
            assert staging.parent == local / 'gvisor/checkpoints'
            def restore(_):
                with Sandbox(snapshot=saved) as clone:
                    assert clone.files.read_text('/workspace/iteration') == str(index)
                    assert saved.verify()['status'] == 'passed'
                    assert not staging.exists()
                    assert clone.run('stat -c %s /workspace/large',check=True).stdout == '33554432\n'
                    clone.files.write_text('/workspace/iteration','clone')
                    again=clone.snapshot(state='filesystem')
                    assert again.verify()['status']=='passed'
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(restore,range(2)))
            assert source.files.read_text('/workspace/iteration')==str(index)
    assert list((local/'gvisor/checkpoints').iterdir()) == []

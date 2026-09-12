"""Real image, restore, termination and pool storage reclamation on two workers."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import threading

import pytest

from sandweave import Memory, Pool, Sandbox
from test_weave_live import cluster, wait_for

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_WEAVE_INTEGRATION'), reason='explicit isolated workers required')]


def test_ephemeral_image_pool_reclaims_payloads_and_preserves_other_sandbox(cluster, tmp_path):
    memory = Memory(guest='256MiB', runtime='256MiB')
    shared = tmp_path/'shared'
    with Sandbox(target='weave-live', memory=memory, startup_timeout=900) as retained:
        retained.files.write_text('/workspace/keep', 'another sandbox')
        saved = retained.cache('retained-baseline', state='filesystem')
        assert saved.verify()['status'] == 'passed'
        for iteration in range(2):
            pool = Pool(target='weave-live', image='docker://busybox:1.37.0',
                        size=2, warm=2, memory=memory, shared_cache=str(shared),
                        retain_baseline=False, wait_timeout=900, startup_timeout=900)
            with pool:
                reference = pool.info['baseline']
                barrier = threading.Barrier(2)
                def episode(index):
                    with pool.acquire() as env:
                        barrier.wait(timeout=60)
                        assert env.run('echo hello').stdout == 'hello\n'
                        env.files.write_text('/value', str(index))
                        return env.id
                with ThreadPoolExecutor(2) as executor:
                    ids = list(executor.map(episode, range(2)))
                assert len(set(ids)) == 2
            info = cluster.connection.call('pool_status', identity=pool.id)
            assert info['state'] == 'closed' and info['artifacts_released']
            assert not info.get('cleanup_error')
            assert not (shared/'pools'/pool.id).exists()
            for connection in cluster.test_workers:
                workspace = Path(connection.call('ping')['workspace'])
                records = [r for r in connection.call('list') if r['spec'].get('_retention_pool') == pool.id]
                local = Path((workspace/'runs/local-path.txt').read_text().strip())
                for record in records:
                    assert record['state'] == 'terminated'
                    assert not (local/'gvisor/bundles'/record['id']).exists()
                assert not (workspace/'snapshots'/reference).exists()
                assert not (local/'gvisor/checkpoints'/reference).exists()
                assert not (workspace/'images/pools'/pool.id).exists()
            assert retained.files.read_text('/workspace/keep') == 'another sandbox'
            assert saved.verify()['status'] == 'passed'
        with Sandbox(target='weave-live', cache=saved, memory=memory, startup_timeout=900) as restored:
            assert restored.files.read_text('/workspace/keep') == 'another sandbox'


def test_failed_builder_also_releases_pool_files(cluster, tmp_path):
    setup = tmp_path/'fail.sh'
    setup.write_text('echo intentional-build-failure >&2\nexit 9\n')
    pool = Pool(target='weave-live', setup=str(setup), retain_baseline=False,
                memory=Memory(guest='256MiB', runtime='256MiB'), wait_timeout=300, startup_timeout=300)
    try:
        with pytest.raises(Exception, match='intentional-build-failure'):
            pool.start()
        wait_for(lambda: cluster.connection.call('pool_status', identity=pool.id)['state'] == 'closed')
        assert cluster.connection.call('pool_status', identity=pool.id)['artifacts_released']
        for connection in cluster.test_workers:
            for record in connection.call('list'):
                if record['spec'].get('_retention_pool') == pool.id:
                    assert record['state'] == 'terminated'
                    assert record['runtime_status']['status'] == 'missing'
    finally:
        pool.close()

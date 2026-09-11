"""Concurrent publication and leases using disposable, real gVisor workers."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from sandweave import Sandbox, Pool, Memory
from sandweave.sandbox.artifacts import Artifacts
from sandweave.sandbox.targets import Endpoint
from test_weave_live import cluster, wait_for, pytestmark


def test_concurrent_import_and_sixteen_live_leases(cluster):
    source, destination = cluster.test_workers
    with Sandbox(target=Endpoint(source.port, source.token), image='docker://busybox:1.37.0',
                 startup_timeout=900) as builder:
        builder.files.write_text('/saved', 'concurrent snapshot')
        saved = builder.snapshot(state='filesystem')
    manifest = source.call('artifact_manifest', reference=saved.id)
    negotiated, published = threading.Barrier(16), threading.Event()
    first_chunk = next((path, source.call('artifact_read', reference=saved.id, path=path,
        offset=0, size=min(1024**2, info['size']))) for path, info in manifest['files'].items()
        if info['kind'] == 'file' and info['size'])

    def transfer(index):
        connection = destination.clone(timeout=900)
        try:
            missing = connection.call('artifact_begin', manifest=manifest)
            negotiated.wait(timeout=60)
            if index == 0:
                for path in missing:
                    size = manifest['files'][path]['size']
                    for offset in range(0, size, 1024**2):
                        data = source.call('artifact_read', reference=saved.id, path=path,
                                           offset=offset, size=min(1024**2, size-offset))
                        connection.call('artifact_write', reference=saved.id, path=path, offset=offset, data=data)
                result = connection.call('artifact_finish', reference=saved.id)
                published.set()
                return result
            assert published.wait(180), 'first importer did not finish'
            path, data = first_chunk
            connection.call('artifact_write', reference=saved.id, path=path, offset=0, data=data)
            return connection.call('artifact_finish', reference=saved.id)
        finally:
            connection.close()

    with ThreadPoolExecutor(16) as executor:
        futures = [executor.submit(transfer, i) for i in range(16)]
        assert [future.result() for future in futures] == [{'id': saved.id}] * 16
    replica = destination.call('artifact_manifest', reference=saved.id)
    assert replica['metadata']['workspace'] != manifest['metadata']['workspace']
    assert Artifacts.content(replica) == Artifacts.content(manifest)
    assert destination.call('artifact_begin', manifest=replica) == []
    with Sandbox(target=Endpoint(destination.port, destination.token), snapshot=saved.id) as restored:
        assert restored.files.read_text('/saved') == 'concurrent snapshot'

    # Register the source through the public cluster lookup. Both workers have
    # the same baseline; 8 slots x 512MiB exactly fits each 4GiB worker budget.
    for worker in cluster.workers:
        cluster.connection.call('worker_update', identity=worker['id'], slots=8)
    with Pool(target='weave-live', cache=saved.id, size=16, warm=16,
              memory=Memory('256MiB', '256MiB'), wait_timeout=900) as pool:
        entered, release = [], threading.Event()
        guard = threading.Lock()
        def episode(index):
            with pool.acquire() as env:
                assert env.files.read_text('/saved') == 'concurrent snapshot'
                env.files.write_text('/episode', str(index))
                assert env.run('cat /episode').stdout == str(index)
                with guard:
                    entered.append(env.id)
                assert release.wait(90), 'peer lease did not become ready'
                return env.id
        with ThreadPoolExecutor(16) as executor:
            futures = [executor.submit(episode, i) for i in range(16)]
            try:
                def ready():
                    for future in futures:
                        if future.done():
                            future.result()  # Preserve the original failure.
                    return len(entered) == 16
                wait_for(ready, timeout=90)
                assert pool.info['active'] == 16
            finally:
                release.set()
            ids = [future.result() for future in futures]
        assert len(set(ids)) == 16
    wait_for(lambda: all(a['released'] for a in cluster.info['sandboxes'] if a['id'] in ids))

"""Shared-cache restoration and locality through the public Pool API."""
from contextlib import ExitStack
from pathlib import Path

from sandweave import Pool, Memory
from sandweave.sandbox.targets import Endpoint
from test_weave_live import cluster, pytestmark, wait_for


def test_shared_pool_baseline_across_independent_workers(cluster):
    cache = Path(cluster.config['directory']).parent / 'shared-images'
    with Pool(target='weave-live', size=4, warm=4, shared_cache=cache,
              affinity='machine', memory=Memory('256MiB', '256MiB')) as pool:
        info = pool.info
        reference = info['baseline']
        assert info['shared_cache'] == str(cache) and info['affinity'] == 'machine'
        locations = {w.call('snapshot_info', reference=reference)['location'] for w in cluster.test_workers}
        assert len(locations) == 1 and Path(next(iter(locations))).is_relative_to(cache)
        assert len({w['machine'] for w in cluster.info['workers']}) == 1
        with ExitStack() as scope:
            envs = [scope.enter_context(pool.acquire()) for _ in range(4)]
            assigned = {cluster.connection.call('allocation_get', identity=e.id)['worker'] for e in envs}
            assert len(assigned) == 2
            for i, env in enumerate(envs):
                env.files.write_text('/workspace/private', str(i))
            assert [e.run('cat /workspace/private').stdout for e in envs] == list(map(str, range(4)))
        with pool.acquire() as fresh:
            assert fresh.run('test ! -e /workspace/private').returncode == 0
    assert (cache / 'artifacts-v1' / reference / 'complete').is_file()


def test_worker_affinity_and_local_pool_use_the_same_cache_contract(cluster):
    cache = Path(cluster.config['directory']).parent / 'worker-images'
    with Pool(target='weave-live', size=2, warm=2, shared_cache=cache,
              affinity='worker', memory=Memory('256MiB', '256MiB')) as pool:
        with ExitStack() as scope:
            envs = [scope.enter_context(pool.acquire()) for _ in range(2)]
            assert len({cluster.connection.call('allocation_get', identity=e.id)['worker'] for e in envs}) == 1
            assert all(e.run('echo cached').stdout == 'cached\n' for e in envs)
        reference = pool.info['baseline']
    wait_for(lambda: all(a['released'] for a in cluster.info['sandboxes']))
    connections = cluster.test_workers
    # Both target endpoints are independent workers with private revision stores.
    with Pool(cache=reference, targets=[Endpoint(c.port, c.token) for c in connections],
              size=2, warm=2, shared_cache=cache) as pool:
        with ExitStack() as scope:
            envs = [scope.enter_context(pool.acquire()) for _ in range(2)]
            assert len({e._connection.port for e in envs}) == 2
            assert all(e.run('echo local').stdout == 'local\n' for e in envs)

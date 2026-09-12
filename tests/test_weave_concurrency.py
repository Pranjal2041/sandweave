"""Regressions for concurrent transfers and stale controller operations."""
import copy
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
import hashlib
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from sandweave.sandbox.artifacts import Artifacts
from sandweave.sandbox.errors import IncompatibleSnapshot, ResourceUnavailable
from sandweave.weave import pool as pools
from test_weave import lab, make_pool, until, request, Executor


@pytest.fixture
def imports(tmp_path):
    store = tmp_path / 'shared-store'
    (store / 'revisions').mkdir(parents=True)
    workers = []
    for name in ('worker-a', 'worker-b'):
        root = tmp_path / name
        root.mkdir()
        workers.append(Artifacts(SimpleNamespace(root=root, store=SimpleNamespace(
            root=store, verify=lambda metadata, **kw: {'status': 'passed'}))))
    identity = 'snap-' + uuid.uuid4().hex
    data = b'one immutable snapshot'
    manifest = {'metadata': {'id': identity, 'digest': 'same-snapshot',
                'workspace': '/source/worker', 'location': '/source/snapshot'},
                'files': {'snapshot/data': {'kind': 'file', 'mode': 0o600,
                    'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'xattrs': {}}}}
    return workers, manifest, data


def test_late_identical_chunk_after_another_importer_finishes(imports):
    (first, second), manifest, data = imports
    identity = manifest['metadata']['id']
    first.begin(manifest)
    second.begin(manifest)
    first.write(identity, 'snapshot/data', 0, data)
    first.finish(identity)
    assert second.write(identity, 'snapshot/data', 0, data) == len(data)
    assert second.finish(identity) == {'id': identity}
    with pytest.raises(IncompatibleSnapshot):
        second.write(identity, 'snapshot/data', 0, b'x' * len(data))
    assert (first.directory(identity) / 'snapshot/data').read_bytes() == data


def test_replica_paths_are_not_part_of_transfer_content_identity(imports):
    (first, second), manifest, data = imports
    identity = manifest['metadata']['id']
    first.begin(manifest)
    replica = copy.deepcopy(manifest)
    replica['metadata'].update(workspace='/replica/import/workspace', location='/replica/import/snapshot')
    assert second.begin(replica) == ['snapshot/data']
    changed = copy.deepcopy(replica)
    changed['files']['snapshot/data']['sha256'] = '0' * 64
    with pytest.raises(IncompatibleSnapshot):
        second.begin(changed)


def test_failed_pool_finishes_existing_waiters_with_original_error(lab):
    pool = make_pool(lab.controller, warm=0)
    lease = uuid.uuid4().hex
    pools.dispatch(lab.controller, 'pool_checkout', dict(identity=pool, lease_id=lease, owner=None))
    state = lab.controller.state
    record = state.get('pool', pool)
    state.put('pool', {**record, 'state': 'failed', 'error': 'image pull was rejected'})
    pools.reconcile(lab.controller)
    result = pools.dispatch(lab.controller, 'pool_lease', dict(identity=pool, lease_id=lease))
    assert (result['state'], result['error']) == ('failed', 'image pull was rejected')


def test_stale_claim_cannot_use_a_termination_generation(lab):
    pool = make_pool(lab.controller, size=1)
    until(lab, lambda: lab.controller.pool_status(pool)['ready'] == 1)
    lease = uuid.uuid4().hex
    pools.dispatch(lab.controller, 'pool_checkout', dict(identity=pool, lease_id=lease, owner=None))
    pools.reconcile(lab.controller)
    saved = lab.controller.state.get('lease', lease)
    assert saved['state'] == 'claiming'
    identity = saved['sandbox']
    pools.dispatch(lab.controller, 'pool_release', dict(identity=pool, lease_id=lease))
    # A claim task queued before release starts after cancellation was saved.
    pools._claim(lab.controller, pool, lease, identity)
    lab.controller._terminate(identity)
    assert lab.controller.allocation_get(identity)['released'] is True


def test_stale_launch_cannot_recreate_a_claimed_pool_member(lab):
    pool = make_pool(lab.controller, size=1)
    until(lab, lambda: lab.controller.pool_status(pool)['ready'] == 1)
    lease = uuid.uuid4().hex
    pools.dispatch(lab.controller, 'pool_checkout', dict(identity=pool, lease_id=lease, owner=None))
    until(lab, lambda: lab.controller.state.get('lease', lease)['state'] == 'ready')
    identity = lab.controller.state.get('lease', lease)['sandbox']
    lab.controller._launch(identity)
    allocation = lab.controller.allocation_get(identity)
    assert allocation['state'] == 'leased', allocation
    assert allocation['error'] is None


def test_equivalent_assignment_replay_ignores_dictionary_order(lab):
    from sandweave.sandbox.sandbox import definition
    worker = lab.executors[1]
    spec = definition(detached=True)['spec']
    original = worker.management.apply('sw-replay', 'cluster', 1, 'create', spec=spec, operation_id='op')
    reordered = dict(reversed(list(spec.items())))
    replay = worker.management.apply('sw-replay', 'cluster', 1, 'create', spec=reordered, operation_id='op')
    assert original == replay and worker.starts == 1
    with pytest.raises(ValueError, match='different operation'):
        worker.management.apply('sw-replay', 'cluster', 1, 'create', spec=reordered, operation_id='changed')


def _import_process(index, root, manifest, data, barrier, published):
    worker_root = root / str(index)
    worker_root.mkdir()
    artifacts = Artifacts(SimpleNamespace(root=worker_root, store=SimpleNamespace(
        root=root / 'shared-store', verify=lambda metadata, **kw: {'status': 'passed'})))
    manifest = copy.deepcopy(manifest)
    manifest['metadata'].update(workspace='/replica/' + str(index), location='/snapshot/' + str(index))
    identity = manifest['metadata']['id']
    assert artifacts.begin(manifest) == ['snapshot/data']
    barrier.wait(timeout=45)
    if index:
        assert published.wait(45)
    artifacts.write(identity, 'snapshot/data', 0, data)
    result = artifacts.finish(identity)
    if index == 0:
        published.set()
    return result


def test_sixteen_processes_import_into_one_store(imports, tmp_path):
    _, manifest, data = imports
    context = multiprocessing.get_context('spawn')
    with context.Manager() as manager:
        barrier, published = manager.Barrier(16), manager.Event()
        with ProcessPoolExecutor(16, mp_context=context) as executor:
            futures = [executor.submit(_import_process, i, tmp_path, manifest, data, barrier, published) for i in range(16)]
            results = [future.result(timeout=60) for future in futures]
    assert results == [{'id': manifest['metadata']['id']}] * 16
    path = tmp_path / 'shared-store/imports' / manifest['metadata']['id'] / 'snapshot/data'
    assert path.read_bytes() == data


def test_64_concurrent_claims_recover_lost_replies(lab, monkeypatch):
    from sandweave import Memory
    from sandweave.sandbox.errors import OperationUnknown
    from sandweave.sandbox.sandbox import definition
    controller = lab.controller
    for index, worker in enumerate(lab.workers, 1):
        controller.worker_update(worker['id'], remove=True)
        controller.worker_add(str(index), slots=32, memory='16GiB')
    pool = make_pool(controller, size=64, warm=0)
    saved = controller.state.get('pool', pool)
    saved['request']['spec']['resources'] = definition(memory=Memory('256MiB', '256MiB'))['spec']['resources']
    controller.state.put('pool', saved)
    original, dropped = Executor.call, set()

    def call(self, operation, **params):
        result = original(self, operation, **params)
        if operation == 'managed_apply' and params['action'] == 'claim' and params['identity'] not in dropped:
            dropped.add(params['identity'])
            raise OperationUnknown('claim reply lost after commit')
        return result

    monkeypatch.setattr(Executor, 'call', call)
    leases = [uuid.uuid4().hex for _ in range(64)]
    with ThreadPoolExecutor(64) as executor:
        futures = [executor.submit(pools.dispatch, controller, 'pool_checkout',
            dict(identity=pool, lease_id=lease, owner=None)) for lease in leases]
        for future in futures:
            future.result()
    until(lab, lambda: all(controller.state.get('lease', lease)['state'] == 'ready' for lease in leases), timeout=45)
    records = [controller.state.get('lease', lease) for lease in leases]
    assert len({r['sandbox'] for r in records}) == len(dropped) == 64
    assert all(not r.get('error') for r in records)
    assert sum(worker.starts for worker in lab.executors.values()) == 64
    pools.dispatch(controller, 'pool_close', {'identity': pool})
    until(lab, lambda: controller.pool_status(pool)['state'] == 'closed', timeout=45)
    assert all(r['released'] for r in controller.state.list('allocation', parent=pool))


def test_confirmed_lost_worker_releases_records_and_cannot_rejoin(lab):
    controller = lab.controller
    identity = request(controller)
    until(lab, lambda: controller.allocation_get(identity)['state'] == 'ready')
    allocation = controller.state.get('allocation', identity)
    worker = controller.state.get('worker', allocation['worker'])
    controller.allocation_cancel(identity)
    with pytest.raises(ResourceUnavailable, match='reserved'):
        controller.worker_update(worker['id'], remove=True)
    # The operator has independently confirmed the execution allocation ended.
    lab.executors[int(worker['target'])].terminate(identity)
    controller.worker_update(worker['id'], remove=True, lost=True)
    record = controller.allocation_get(identity)
    assert record['released'] and record['state'] == 'failed'
    controller._uncertain(identity, RuntimeError('late reply'), generation=allocation['generation'])
    controller._observe(identity)
    controller._terminate(identity)
    assert controller.allocation_get(identity) == record
    with pytest.raises(ResourceUnavailable, match='declared lost'):
        controller.worker_add(worker['target'], slots=1, memory='4GiB')


def test_failed_start_preserves_error_when_cleanup_cannot_reach_controller():
    pool = pools.ManagedPool.__new__(pools.ManagedPool)
    pool._declare = lambda: None
    pool.initial_ready, pool.owned, pool.wait_timeout, pool.id = False, True, 300, 'pool-failed'
    calls = []
    def call(operation, **params):
        calls.append(operation)
        if operation == 'pool_status':
            return {'state': 'failed', 'error': 'registry authentication failed'}
        raise ConnectionError('controller disappeared')
    pool.connection = SimpleNamespace(call=call)
    with pytest.raises(ResourceUnavailable, match='registry authentication failed'):
        pool.start()
    assert calls == ['pool_status', 'pool_close']


def test_failed_context_requests_cleanup_without_waiting_for_dead_workers():
    pool = pools.ManagedPool.__new__(pools.ManagedPool)
    pool.owned, pool.started, pool.closed, pool.id = True, True, False, 'pool-failed'
    calls = []
    pool.connection = SimpleNamespace(call=lambda operation, **params: calls.append(operation),
                                      close=lambda: calls.append('disconnect'))
    error = ResourceUnavailable('image import failed')
    assert pool.__exit__(type(error), error, None) is None
    assert calls == ['pool_close', 'disconnect'] and pool.closed


def test_failed_pool_settles_claiming_lease_but_preserves_issued_lease(lab):
    controller = lab.controller
    pool = make_pool(controller, size=2, warm=2)
    until(lab, lambda: controller.pool_status(pool)['ready'] == 2)
    first, second = uuid.uuid4().hex, uuid.uuid4().hex
    pools.dispatch(controller, 'pool_checkout', dict(identity=pool, lease_id=first, owner=None))
    until(lab, lambda: controller.state.get('lease', first)['state'] == 'ready')
    pools.dispatch(controller, 'pool_checkout', dict(identity=pool, lease_id=second, owner=None))
    pools.reconcile(controller)
    assert controller.state.get('lease', second)['state'] == 'claiming'
    pools._fail(controller, pool, 'source is unavailable')
    for lease, expected in [(first, 'ready'), (second, 'failed')]:
        assert controller.state.get('lease', lease)['state'] == expected
    sid = controller.state.get('lease', second)['sandbox']
    pools._claim(controller, pool, second, sid)
    until(lab, lambda: controller.allocation_get(sid)['released'])
    pools.dispatch(controller, 'pool_release', dict(identity=pool, lease_id=second))
    pools.dispatch(controller, 'pool_close', dict(identity=pool))
    assert controller.state.get('lease', second)['error'] == 'source is unavailable'
    assert controller.state.get('lease', second)['state'] == 'failed'


def test_cleanup_does_not_erase_the_last_successful_preparation(lab):
    controller = lab.controller
    pool = make_pool(controller, size=1, warm=0)
    record = controller.state.get('pool', pool)
    # Earlier failed attempts must not become consecutive failures again when
    # the successful member is later discarded after an episode.
    for _ in range(3):
        pools._new_member(controller, record)
        failed = controller.state.list('allocation', parent=pool)[-1]
        controller.state.put('allocation', {**failed, 'state': 'failed', 'desired': 'terminated',
                                            'released': True, 'error': 'old preparation failed'})
    pools._new_member(controller, record)
    successful = controller.state.list('allocation', parent=pool)[-1]
    controller.state.put('allocation', {**successful, 'state': 'reserved', 'worker': lab.workers[0]['id']})
    controller._launch(successful['id'])
    # Image preparation now yields the launch thread. Resume after the
    # shared dependency finishes, without reconciling the synthetic history.
    pending = controller.preparation.waiters[successful['id']][1]
    pending.result(timeout=5)
    controller._launch(successful['id'])
    assert controller.allocation_get(successful['id'])['state'] == 'ready'
    controller.allocation_cancel(successful['id'])
    controller._terminate(successful['id'])
    pools.reconcile(controller)
    assert controller.pool_status(pool)['state'] == 'ready'

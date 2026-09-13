"""Pool cleanup after lost replies and reuse without shared-storage I/O."""
from types import SimpleNamespace
import threading

import pytest

from sandweave import Memory
from sandweave.sandbox.errors import CacheMiss, OperationUnknown
from sandweave.sandbox.sandbox import definition
from sandweave.weave import pool as pools, artifacts
from sandweave.weave.controller import Controller
from test_weave import lab, make_pool, until, Executor, request


def client_pool(call):
    pool = pools.ManagedPool.__new__(pools.ManagedPool)
    pool.id, pool.wait_timeout = 'pool-client', .2
    pool.start = lambda: pool
    pool.connection = SimpleNamespace(call=call)
    return pool


def test_lost_checkout_reply_still_releases_committed_lease(lab, monkeypatch):
    controller = lab.controller
    identity = make_pool(controller, warm=0)
    releases = []
    def call(operation, **params):
        if operation == 'pool_release':
            releases.append(params['lease_id'])
            if len(releases) < 3:
                raise OperationUnknown('release was not delivered')
        result = pools.dispatch(controller, operation, params)
        if operation == 'pool_checkout':
            raise OperationUnknown('checkout reply lost after commit')
        return result
    pool = client_pool(call)
    pool.id, pool.wait_timeout = identity, 2
    monkeypatch.setattr(pools, 'client_owner', lambda connection: None)
    with pytest.raises(OperationUnknown, match='checkout reply lost'):
        with pool.acquire():
            pytest.fail('checkout did not return')
    assert len(releases) == 3 and len(set(releases)) == 1
    lease = controller.state.get('lease', releases[0])
    assert lease['state'] == 'cancelled'
    assert controller.pool_status(identity)['waiting'] == 0


def test_release_retries_a_lost_acknowledgement(lab):
    controller = lab.controller
    identity = make_pool(controller, warm=0)
    lease = 'lease-lost-release'
    pools.dispatch(controller, 'pool_checkout', dict(identity=identity, lease_id=lease, owner=None))
    calls = []
    def call(operation, **params):
        result = pools.dispatch(controller, operation, params)
        calls.append(operation)
        if len(calls) <= 3:
            raise OperationUnknown('release reply lost after commit')
        return result
    pool = client_pool(call)
    pool.id, pool.wait_timeout = identity, 2
    assert pool._release_lease(lease)['state'] == 'released'
    assert calls == ['pool_release'] * 4
    assert controller.state.get('lease', lease)['state'] == 'cancelled'


def test_release_retry_has_its_own_bounded_deadline(monkeypatch):
    now, delays = [10.0], []
    def sleep(delay):
        delays.append(delay)
        now[0] += delay
    monkeypatch.setattr(pools, 'time', SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))
    def unavailable(*args, **kwargs):
        raise OperationUnknown('controller unreachable')
    pool = client_pool(unavailable)
    with pytest.raises(OperationUnknown, match='controller unreachable'):
        pool._release_lease('lease')
    assert sum(delays) == pytest.approx(.2)
    assert max(delays) <= .05


def test_poll_wait_clips_deadline_and_observes_cancellation(monkeypatch):
    sleeps, waits = [], []
    monkeypatch.setattr(pools, 'time', SimpleNamespace(monotonic=lambda: 10, sleep=sleeps.append))
    pools._poll_wait(10.01)
    pools._poll_wait(None, SimpleNamespace(wait=waits.append))
    assert sleeps == pytest.approx([.01]) and waits == [.05]
    # A set cancellation event returns immediately instead of sleeping.
    cancelled = threading.Event()
    cancelled.set()
    pools._poll_wait(None, cancelled)


def test_native_revision_is_registered_without_touching_shared_cache(lab, monkeypatch):
    controller = lab.controller
    endpoint = controller.state.get('worker', lab.workers[0]['id'])['endpoint']
    reference = 'snap-' + 'b' * 32
    original, calls = Executor.call, []
    def call(self, operation, **params):
        calls.append(operation)
        if operation == 'snapshot_info':
            return {'id': reference, 'state': 'filesystem'}
        if operation == 'snapshot_spec':
            return {'reference': reference, 'spec': definition()['spec']}
        assert not operation.startswith('artifact_')
        return original(self, operation, **params)
    monkeypatch.setattr(Executor, 'call', call)
    connection = controller.connection(endpoint)
    try:
        assert controller.preparation.request({'request': {'reference': reference}}, connection,
                                              endpoint, '/unavailable/shared') == reference
        assert controller.state.get('artifact', reference)['locations'] == [endpoint]
        assert not controller.preparation.transfers and not controller.preparation.waiters
        controller.state.put('artifact', {**controller.state.get('artifact', reference), 'retiring': True})
        with pytest.raises(CacheMiss, match='being released'):
            controller.preparation.request({'request': {'reference': reference}}, connection,
                                           endpoint, '/unavailable/shared')
    finally:
        connection.close()
    assert calls.count('snapshot_info') == 3


def test_128_native_launches_need_no_publication_or_transfer(lab, monkeypatch):
    controller = lab.controller
    for worker in lab.workers:
        controller.worker_update(worker['id'], remove=True)
        controller.worker_add(str(lab.workers.index(worker) + 1), slots=128, memory='16GiB')
    pool = make_pool(controller, size=128, warm=128)
    record = controller.state.get('pool', pool)
    reference = record['baseline']
    record['request']['spec']['resources'] = definition(memory=Memory('64MiB', '64MiB'))['spec']['resources']
    controller.state.put('pool', {**record, 'shared_cache': '/unavailable/shared'})
    controller.state.put('artifact', dict(id=reference, info={'id': reference},
        locations=[controller.state.get('worker', w['id'])['endpoint'] for w in lab.workers]))
    original = Executor.call
    def call(self, operation, **params):
        if operation == 'snapshot_info':
            return {'id': reference}
        assert not operation.startswith('artifact_'), 'native hit touched shared storage'
        return original(self, operation, **params)
    monkeypatch.setattr(Executor, 'call', call)
    def unexpected(*args):
        pytest.fail('native revision was transferred')
    monkeypatch.setattr(artifacts, 'ensure', unexpected)
    until(lab, lambda: controller.pool_status(pool)['ready'] == 128, timeout=15)
    assert sum(e.starts for e in lab.executors.values()) == 128
    assert not controller.preparation.transfers and not controller.preparation.waiters
    pools.dispatch(controller, 'pool_close', {'identity': pool})
    until(lab, lambda: controller.pool_status(pool)['state'] == 'closed', timeout=15)


@pytest.mark.parametrize('affinity', ['worker', 'machine'])
@pytest.mark.parametrize('restart', [False, True])
def test_controller_keeps_released_builder_affinity_without_charging_it(lab, affinity, restart):
    controller = lab.controller
    fleet = sorted(controller.state.list('worker'), key=lambda w: w['id'])
    for index, worker in enumerate(fleet):
        controller.state.put('worker', {**worker, 'machine': 'machine-' + str(index)})
    pool = make_pool(controller, size=1, warm=0)
    builder = request(controller)
    saved = controller.state.get('allocation', builder)
    controller.state.put('allocation', {**saved, 'parent': pool, 'role': 'builder',
        'worker': fleet[1]['id'], 'state': 'terminated', 'desired': 'terminated',
        'released': True, 'prepared': True})
    record = controller.state.get('pool', pool)
    controller.state.put('pool', {**record, 'builder': builder, 'affinity': affinity, 'warm': 1})
    if restart:
        controller.close()
        lab.controller = controller = Controller(lab.directory)
    # Stop probes rewriting the test's distinct machine IDs from fake inventory.
    controller.next_probe = float('inf')
    controller.tick()
    members = [a for a in controller.state.list('allocation', parent=pool) if a.get('role') == 'member']
    assert len(members) == 1 and members[0]['state'] == 'reserved'
    assert members[0]['worker'] == fleet[1]['id']
    # The preferred worker has one slot: charging its released builder would
    # force this member onto the other worker.
    assert controller.state.get('worker', fleet[1]['id'])['capacity']['slots'] == 1

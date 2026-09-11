"""A living cluster client can return after its worker lease has expired."""
from concurrent.futures import ThreadPoolExecutor
import time
import uuid

import pytest

from sandweave.sandbox import ownership
from sandweave.sandbox.errors import OwnerExpired
from sandweave.sandbox.ownership import Owners
from sandweave.sandbox.sandbox import definition
from sandweave.weave.pool import dispatch as pool_call
from test_weave import lab, make_pool, until


def test_pool_reacquires_after_last_release_and_remote_grace(lab, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(ownership.time, 'time', lambda: now[0])
    controller = lab.controller
    controller.worker_update(lab.workers[1]['id'], draining=True)
    owner = uuid.uuid4().hex
    controller.owner_register(owner, None)  # No worker-local PID identity.
    pool = make_pool(controller, size=1, warm=0, owner=owner)
    first = uuid.uuid4().hex
    pool_call(controller, 'pool_checkout', dict(identity=pool, lease_id=first, owner=owner))
    until(lab, lambda: controller.state.get('lease', first)['state'] == 'ready')
    old = controller.state.get('lease', first)['sandbox']
    worker = lab.executors[1]
    old_owner = worker.read(old)['owner']
    pool_call(controller, 'pool_release', dict(identity=pool, lease_id=first))
    until(lab, lambda: controller.allocation_get(old)['released'])
    for _ in range(ownership.GRACE_SECONDS // ownership.HEARTBEAT_SECONDS + 2):
        now[0] += ownership.HEARTBEAT_SECONDS
        assert controller.owner_heartbeat(owner)['routes'] == []
    assert worker.owners.reason(old_owner) == 'owner_heartbeat_expired'
    second = uuid.uuid4().hex
    pool_call(controller, 'pool_checkout', dict(identity=pool, lease_id=second, owner=owner))
    until(lab, lambda: controller.state.get('lease', second)['state'] in ('ready', 'failed'))
    lease = controller.state.get('lease', second)
    assert lease['state'] == 'ready', lease.get('error')
    assert worker.read(lease['sandbox'])['owner'] != old_owner
    assert worker.owners.reason(old_owner) == 'owner_heartbeat_expired'


def test_new_assignment_cannot_revive_an_expired_assignment(lab, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(ownership.time, 'time', lambda: now[0])
    worker = lab.executors[1]
    owner = uuid.uuid4().hex
    request = dict(cluster='cluster', generation=1, action='create',
                   spec=definition()['spec'], owner=owner, process=None)
    first = worker.management.apply('sw-old', **request)
    old_owner = worker.read('sw-old')['owner']
    now[0] += ownership.GRACE_SECONDS + 10
    assert worker.expiry_reason(worker.read('sw-old')) == 'owner_heartbeat_expired'
    fresh = worker.management.apply('sw-new', **request)
    new_owner = worker.read('sw-new')['owner']
    assert new_owner != old_owner
    assert worker.management.authorize(fresh['token'], 'owner_heartbeat', {'identity': owner})
    assert not worker.management.authorize(fresh['token'], 'owner_heartbeat', {'identity': uuid.uuid4().hex})
    # Clients retain the logical owner ID and one heartbeat per worker.
    for _ in range(ownership.GRACE_SECONDS // ownership.HEARTBEAT_SECONDS + 2):
        now[0] += ownership.HEARTBEAT_SECONDS
        worker.owner_heartbeat(owner)
    assert worker.expiry_reason(worker.read('sw-new')) is None
    assert worker.expiry_reason(worker.read('sw-old')) == 'owner_heartbeat_expired'
    with pytest.raises(OwnerExpired):
        worker.management.apply('sw-old', **request)
    assert worker.management.read('sw-old')['token'] in first['token']
    worker.management.apply('sw-old', 'cluster', 2, 'terminate')
    assert worker.read('sw-old')['state'] == 'terminated'
    assert worker.read('sw-new')['state'] == 'ready'
    now[0] += ownership.GRACE_SECONDS + 10  # A real crash must still expire the replacement lease.
    assert worker.expiry_reason(worker.read('sw-new')) == 'owner_heartbeat_expired'


def test_legacy_expired_worker_owner_does_not_poison_new_assignment(lab, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(ownership.time, 'time', lambda: now[0])
    worker = lab.executors[1]
    owner = uuid.uuid4().hex
    worker.owner_register(owner, None)
    now[0] += ownership.GRACE_SECONDS + 10
    assert worker.owners.reason(owner) == 'owner_heartbeat_expired'
    worker.management.apply('sw-new', 'cluster', 1, 'create',
                            spec=definition()['spec'], owner=owner, process=None)
    assert worker.read('sw-new')['owner'] != owner
    assert worker.owners.reason(owner) == 'owner_heartbeat_expired'
    worker.owner_heartbeat(owner)


def test_managed_lease_is_shared_persisted_and_rotated_atomically(tmp_path, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(ownership.time, 'time', lambda: now[0])
    owners = Owners(tmp_path)
    identity = uuid.uuid4().hex
    with ThreadPoolExecutor(16) as executor:
        leases = list(executor.map(lambda _: owners.register_managed(identity, None), range(32)))
    assert len(set(leases)) == 1
    previous = leases[0]
    owners = Owners(tmp_path)
    assert owners.register_managed(identity, None) == previous
    now[0] += ownership.GRACE_SECONDS + 10
    with ThreadPoolExecutor(16) as executor:
        leases = list(executor.map(lambda _: owners.register_managed(identity, None), range(32)))
    assert len(set(leases)) == 1 and leases[0] != previous
    owners = Owners(tmp_path)
    owners.heartbeat_managed(identity)
    with pytest.raises(OwnerExpired):
        owners.heartbeat(previous)
    with pytest.raises(ValueError, match='already in use'):
        owners.register_managed(identity, ownership.process_identity())


def test_live_legacy_leases_keep_renewing_and_other_owners_still_expire(tmp_path, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(ownership.time, 'time', lambda: now[0])
    owners = Owners(tmp_path)
    identity, other = uuid.uuid4().hex, uuid.uuid4().hex
    owners.register(identity, None)
    assert owners.register_managed(identity, None) == identity
    other_lease = owners.register_managed(other, None)
    for _ in range(ownership.GRACE_SECONDS // ownership.HEARTBEAT_SECONDS + 2):
        now[0] += ownership.HEARTBEAT_SECONDS
        owners.heartbeat_managed(identity)
    assert owners.reason(identity) is None
    assert owners.reason(other_lease) == 'owner_heartbeat_expired'


def test_claim_retry_keeps_its_original_lease(lab, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(ownership.time, 'time', lambda: now[0])
    worker = lab.executors[1]
    owner = uuid.uuid4().hex
    worker.management.apply('sw-member', 'cluster', 1, 'create', spec=definition(detached=True)['spec'])
    request = dict(identity='sw-member', cluster='cluster', generation=2, action='claim',
                   spec=definition()['spec'], owner=owner, process=None)
    response = worker.management.apply(**request)
    old_owner = worker.read('sw-member')['owner']
    assert worker.management.apply(**request)['token'] == response['token']
    assert worker.read('sw-member')['owner'] == old_owner
    now[0] += ownership.GRACE_SECONDS + 10
    worker.management.apply('sw-new', 'cluster', 1, 'create', spec=definition()['spec'], owner=owner)
    with pytest.raises(OwnerExpired):
        worker.management.apply(**request)
    assert worker.expiry_reason(worker.read('sw-member')) == 'owner_heartbeat_expired'

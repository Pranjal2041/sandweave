"""Ownership expiry, persistence and lifecycle interactions without a runtime."""
import copy
import os
import threading
import time
from types import SimpleNamespace
import uuid

import pytest

from sandweave import Sandbox, SandboxError
from sandweave.sandbox import ownership
from sandweave.sandbox.ownership import Owners, process_alive, process_identity
from sandweave.sandbox.worker import Worker


def test_local_identity_survives_missed_heartbeats_and_detects_pid_reuse(tmp_path, monkeypatch):
    process = process_identity()
    assert process and process_alive(process) is True
    owners = Owners(tmp_path)
    identity = uuid.uuid4().hex
    owners.register(identity, process)
    later = time.time() + 1000
    monkeypatch.setattr(ownership.time, 'time', lambda: later)
    assert owners.reason(identity) is None
    assert Owners(tmp_path).reason(identity) is None
    reused = {**process, 'started': str(int(process['started']) + 1)}
    assert process_alive(reused) is False
    identity = uuid.uuid4().hex
    owners.register(identity, reused)
    assert owners.reason(identity) == 'owner_exited'


def test_remote_renewal_expiry_and_restart_are_final(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(ownership.time, 'time', lambda: now[0])
    owners = Owners(tmp_path)
    identity = uuid.uuid4().hex
    owners.register(identity, None)
    now[0] = 125
    owners.heartbeat(identity)
    owners = Owners(tmp_path)
    now[0] = 154
    assert owners.reason(identity) is None
    now[0] = 155
    assert owners.reason(identity) == 'owner_heartbeat_expired'
    # Even a clock adjustment or a late renewal cannot undo decided cleanup.
    now[0] = 120
    owners = Owners(tmp_path)
    with pytest.raises(SandboxError, match='no longer active'):
        owners.heartbeat(identity)
    with pytest.raises(SandboxError, match='no longer active'):
        owners.register(identity, None)


def test_other_host_or_pid_namespace_uses_heartbeat_not_local_pid(tmp_path):
    process = process_identity()
    process['scope'] = {**process['scope'], 'boot': 'another-host-boot'}
    process['started'] = '0'
    assert process_alive(process) is None
    owners = Owners(tmp_path)
    identity = uuid.uuid4().hex
    owners.register(identity, process)
    assert owners.reason(identity) is None


def test_zombie_owner_is_dead_even_before_parent_waits(monkeypatch):
    process = process_identity()
    monkeypatch.setattr(ownership, 'process_state', lambda pid: ('Z', process['started']))
    assert process_alive(process) is False


def test_process_name_does_not_need_to_be_utf8(monkeypatch):
    value = b'123 (a) b\xff) S ' + b'0 ' * 18 + b'456 0\n'
    monkeypatch.setattr(ownership.Path, 'read_bytes', lambda path: value)
    assert ownership.process_state(123) == ('S', '456')


def test_full_metadata_disk_does_not_prevent_owner_cleanup(tmp_path, monkeypatch):
    owners = Owners(tmp_path)
    identity = uuid.uuid4().hex
    owners.register(identity, None)
    later = time.time() + 1000
    monkeypatch.setattr(ownership.time, 'time', lambda: later)

    def full(*args):
        raise OSError('disk full')

    monkeypatch.setattr(ownership, 'atomic_json', full)
    assert owners.reason(identity) == 'owner_heartbeat_expired'
    with pytest.raises(SandboxError):
        owners.heartbeat(identity)


class Runtime:
    def __init__(self):
        self.states = {}
        self.fail_termination = set()

    def status(self, identity):
        return {'status': self.states.get(identity, 'stopped')}

    def terminate(self, identity):
        if identity in self.fail_termination:
            raise RuntimeError('temporary cleanup failure')
        self.states[identity] = 'stopped'


@pytest.fixture
def worker(tmp_path):
    worker = Worker.__new__(Worker)
    worker.root = tmp_path
    worker.records = tmp_path / 'sandboxes'
    worker.records.mkdir()
    worker.owners = Owners(tmp_path)
    worker.runtime = Runtime()
    worker.guard, worker.locks = threading.RLock(), {}
    worker.controls, worker.deadlines = {}, {}
    return worker


def record(worker, *, state='ready', owner=None, **fields):
    identity = 'sw-' + uuid.uuid4().hex
    worker.write({'id': identity, 'state': state, 'owner': owner, 'spec': {}, **fields})
    worker.runtime.states[identity] = 'paused' if state == 'paused' else 'running'
    return identity


@pytest.mark.parametrize('state', ['creating', 'preparing', 'ready', 'paused', 'failed'])
def test_owner_cleanup_covers_all_live_states_and_preserves_unowned(worker, state):
    identity = record(worker, state=state, owner=uuid.uuid4().hex)
    # Old records and detached instances have no owner; never infer ownership.
    detached = record(worker)
    legacy = record(worker)
    value = worker.read(legacy)
    value.pop('owner')
    worker.write(value)
    worker.expire_once()
    assert worker.read(identity)['termination_reason'] == 'owner_lost'
    assert worker.runtime.status(identity)['status'] == 'stopped'
    for kept in (detached, legacy):
        assert worker.read(kept)['state'] == 'ready'
        assert worker.runtime.status(kept)['status'] == 'running'


def test_ttl_still_expires_detached_and_paused_instances(worker):
    identity = record(worker, state='paused', expires_at=time.time() - 1)
    worker.expire_once()
    assert worker.read(identity)['termination_reason'] == 'ttl'


def test_cleanup_retries_failure_and_does_not_block_on_another_operation(worker):
    owner = uuid.uuid4().hex
    busy = record(worker, owner=owner)
    failed = record(worker, owner=owner)
    available = record(worker, owner=owner)
    worker.runtime.fail_termination.add(failed)
    held, release = threading.Event(), threading.Event()

    def hold():
        with worker.lock(busy):
            held.set()
            assert release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert held.wait(5)
        worker.expire_once()
        assert worker.read(available)['state'] == 'terminated'
        assert worker.read(failed)['state'] == 'ready'
        assert worker.read(busy)['state'] == 'ready'
    finally:
        release.set()
        thread.join(5)
    worker.runtime.fail_termination.clear()
    worker.expire_once()
    assert worker.read(busy)['state'] == worker.read(failed)['state'] == 'terminated'


def test_cleanup_keeps_releasing_resources_when_metadata_disk_is_full(worker, monkeypatch):
    from sandweave.sandbox import worker as module
    identities = [record(worker, owner=uuid.uuid4().hex) for _ in range(2)]

    def full(*args):
        raise OSError('disk full')

    monkeypatch.setattr(worker, 'write', full)
    monkeypatch.setattr(module, 'atomic_json', full)
    worker.expire_once()
    assert all(worker.runtime.status(identity)['status'] == 'stopped' for identity in identities)


def test_startup_checks_owner_without_blocking_cleanup_hooks(worker):
    identity = record(worker, state='preparing', owner=uuid.uuid4().hex)
    worker.deadlines[identity] = time.monotonic() + 60
    with pytest.raises(SandboxError, match='owner_lost'):
        worker.remaining(identity)
    worker.deadlines.pop(identity)
    # Cleanup hooks may run guest commands after startup has been cancelled.
    assert worker.remaining(identity, 5) == 5


def test_sdk_detached_defaults_and_borrowed_contexts(monkeypatch):
    from sandweave.sandbox import sandbox
    calls = []
    infos = {}

    class Connection:
        def clone(self, *, timeout=None):
            return self

        def call(self, operation, **params):
            calls.append((operation, params))
            if operation == 'snapshot_spec':
                return {'reference': 'snapshot', 'spec': copy.deepcopy(infos['saved']['spec'])}
            if operation == 'create':
                info = {'id': params['identity'], 'spec': params['spec'], 'state': 'ready', 'capabilities': {}}
                infos[info['id']] = info
                return info
            return infos[params['identity']]

        def close(self):
            pass

    connection = Connection()
    monkeypatch.setattr(sandbox, 'connect', lambda *a, **kw: connection)
    monkeypatch.setattr(ownership, 'client_owner', lambda conn: 'test-owner')
    env = Sandbox()
    assert calls[-1][1]['owner'] == 'test-owner'
    assert env.spec['detached'] is False
    before = len(calls)
    env.close()
    assert len(calls) == before
    with Sandbox.connect(env.id):
        pass
    assert calls[-1][0] == 'describe'
    with Sandbox(detached=True) as detached:
        assert calls[-1][1]['owner'] is None
        infos['saved'] = infos[detached.id]
    assert calls[-1][0] == 'terminate'
    # A clone's lifetime belongs to its caller, not its saved source.
    clone = Sandbox(snapshot='snapshot')
    assert calls[-1][1]['owner'] == 'test-owner'
    assert clone.spec['detached'] is False
    before = len(calls)
    with pytest.raises(ValueError, match='detached must be a bool'):
        Sandbox(detached='false')
    assert len(calls) == before


def test_client_owner_shares_one_channel_and_resets_after_fork(monkeypatch):
    created = []

    class Client:
        def __init__(self, connection):
            self.id, self.expired = uuid.uuid4().hex, False
            created.append(self)

    monkeypatch.setattr(ownership, '_clients', {})
    monkeypatch.setattr(ownership, '_clients_lock', threading.Lock())
    monkeypatch.setattr(ownership, 'ClientOwner', Client)
    connection = SimpleNamespace(host='localhost', port=42, token='private', unix_path=None)
    first = ownership.client_owner(connection)
    assert ownership.client_owner(connection) == first
    assert len(created) == 1
    ownership._after_fork()
    assert ownership.client_owner(connection) != first
    assert len(created) == 2


def test_heartbeat_retries_worker_errors_until_explicit_expiry(monkeypatch):
    from sandweave.sandbox.errors import OwnerExpired
    attempts, closed = [], []

    def call(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise SandboxError('temporary worker error')
        raise OwnerExpired('owner expired')

    owner = ownership.ClientOwner.__new__(ownership.ClientOwner)
    owner.id, owner.pid, owner.expired, owner.interval = 'test', os.getpid(), False, 0
    owner.connection = SimpleNamespace(call=call, close=lambda: closed.append(1))
    owner._heartbeat()
    assert owner.expired and len(attempts) == len(closed) == 2


def test_cli_create_is_detached_but_run_keeps_its_owned_scope(monkeypatch):
    from sandweave import cli
    options = []

    class Env:
        id = 'test-env'

        def __init__(self, **kwargs):
            options.append(kwargs)

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(cli, 'Sandbox', Env)
    monkeypatch.setattr(cli, 'execute', lambda env, args: 0)
    assert cli.main(['create']) == 0
    assert options[-1]['detached'] is True
    assert cli.main(['run', '--', 'true']) == 0
    assert not options[-1].get('detached', False)

"""Coordination and failure semantics using independent in-memory executors."""
import copy
import hashlib
import json
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace
import uuid

import pytest

from sandweave.sandbox.errors import OperationUnknown, ResourceUnavailable
from sandweave.sandbox.management import Management
from sandweave.sandbox.ownership import Owners, process_identity
from sandweave.sandbox.sandbox import definition
from sandweave.sandbox.worker import Worker
from sandweave.weave.controller import Controller
from sandweave.weave import providers, scheduler, artifacts
from sandweave.weave.pool import dispatch as pool_call
from sandweave.weave.state import State


class Executor(Worker):
    """No guest execution: exercise real worker journals and controller protocol."""
    def __init__(self, root, index):
        self.root = root
        root.mkdir()
        self.records = root / 'sandboxes'
        self.records.mkdir()
        self.owners = Owners(root)
        self.guard, self.locks = threading.RLock(), {}
        self.management = Management(self)
        self.index = index
        self.starts = 0
        self.drop_reply = False

    def create(self, spec, identity, **options):
        self.starts += 1
        self.write(dict(id=identity, state='ready', spec=spec, owner=options.get('owner'),
                        operation_id=options.get('operation_id'), timings={}, capabilities={}))
        return self.describe(identity)

    def describe(self, identity):
        value = self.read(identity)
        return {**value, 'runtime_status': {'status': 'running' if value['state'] == 'ready' else 'stopped'},
                'worker': {'hostname': socket.gethostname()}}

    def terminate(self, identity):
        value = self.read(identity)
        self.write({**value, 'state': 'terminated'})
        return self.describe(identity)

    def call(self, operation, **params):
        if operation == 'inventory':
            return dict(protocol=1, hostname=socket.gethostname(), workspace=str(self.root),
                        scope={'boot': 'test'}, cpus=[self.index], memory=16*1024**3,
                        gpus=[], runtimes=['gvisor'], live=[])
        if operation == 'ping':
            return dict(hostname=socket.gethostname(), port=self.index, workspace=str(self.root))
        if operation == 'managed_prepare':
            return {'information': self.call('ping'), 'token': 'private-worker-token'}
        result = self.dispatch(operation, params)
        if operation == 'managed_apply' and self.drop_reply:
            self.drop_reply = False
            raise OperationUnknown('injected lost response after worker commit')
        return result


class Link:
    def __init__(self, executor):
        self.executor = executor
        self.token = 'private-worker-token'

    def call(self, op, **params):
        return self.executor.call(op, **params)

    def close(self):
        pass


@pytest.fixture
def lab(tmp_path, monkeypatch):
    executors = {i: Executor(tmp_path / str(i), i) for i in (1, 2)}
    monkeypatch.setattr(providers, 'direct', lambda endpoint, **kw: Link(executors[endpoint['port']]))
    monkeypatch.setattr(artifacts, 'ensure', lambda *a: None)
    controller = Controller(tmp_path / 'control', connector=lambda target, **kw: Link(executors[int(target)]))
    workers = [controller.worker_add(str(i), slots=1, memory='4GiB') for i in executors]
    value = SimpleNamespace(controller=controller, executors=executors, workers=workers, directory=tmp_path / 'control')
    yield value
    value.controller.close()


def until(lab, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        lab.controller.next_probe = 0
        lab.controller.tick()
        if time.monotonic() >= deadline:
            raise AssertionError('coordination did not converge: ' + repr(lab.controller.status()))
        time.sleep(.005)


def request(controller, identity=None):
    identity = identity or 'sw-' + uuid.uuid4().hex
    controller.create(identity, **definition(detached=True), operation_id=uuid.uuid4().hex)
    return identity


def test_reservations_queue_then_release_without_oversubscription(lab):
    ids = [request(lab.controller) for _ in range(3)]
    until(lab, lambda: sum(lab.controller.allocation_get(i)['state'] == 'ready' for i in ids) == 2)
    pending = next(i for i in ids if lab.controller.allocation_get(i)['state'] == 'pending')
    assert 'reserved' in lab.controller.allocation_get(pending)['reason']
    first = next(i for i in ids if lab.controller.allocation_get(i)['state'] == 'ready')
    lab.controller.allocation_cancel(first)
    until(lab, lambda: lab.controller.allocation_get(pending)['state'] == 'ready')
    assert lab.controller.allocation_get(first)['released'] is True
    assert sum(e.starts for e in lab.executors.values()) == 3


def test_lost_launch_reply_recovers_same_sandbox_and_access_token(lab):
    for executor in lab.executors.values():
        executor.drop_reply = True
    identity = request(lab.controller)
    until(lab, lambda: lab.controller.allocation_get(identity)['state'] == 'ready' and
          lab.controller.state.get('allocation', identity).get('token'))
    assert sum(e.starts for e in lab.executors.values()) == 1
    route = lab.controller.allocation_ack(identity)
    assert route['endpoint']['token'].startswith('sw1.' + identity)


def test_cancelled_assignment_rejects_delayed_create_and_cross_cluster(lab):
    worker = lab.executors[1]
    identity = 'sw-cancelled'
    worker.management.apply(identity, 'first', 2, 'terminate')
    with pytest.raises(OperationUnknown):
        worker.management.apply(identity, 'first', 1, 'create', spec=definition(detached=True)['spec'])
    with pytest.raises(PermissionError):
        worker.management.apply(identity, 'second', 3, 'create', spec=definition(detached=True)['spec'])
    assert worker.starts == 0


def test_scoped_token_cannot_control_another_sandbox_or_worker(lab):
    worker = lab.executors[1]
    response = worker.management.apply('sw-owned', 'cluster', 1, 'create',
        spec=definition(detached=True)['spec'], operation_id='operation')
    token = response['token']
    assert worker.management.authorize(token, 'describe', {'identity': 'sw-owned'})
    assert not worker.management.authorize(token, 'describe', {'identity': 'sw-other'})
    assert not worker.management.authorize(token, 'managed_apply', {'identity': 'sw-owned'})
    assert not worker.management.authorize(token, 'managed_prepare', {})
    assert not worker.management.authorize(token, 'list', {})
    assert not worker.management.authorize(token + 'bad', 'describe', {'identity': 'sw-owned'})


def make_pool(controller, *, size=2, warm=1, owner=None):
    identity = 'pool-' + uuid.uuid4().hex
    source = definition(detached=True)
    source['reference'] = 'snap-' + 'a'*32
    pool_call(controller, 'pool_create', dict(identity=identity, name=None, request=source,
        size=size, warm=warm, weight=1, priority=0, labels={}, placement='spread', owner=owner))
    return identity


def test_pool_claims_are_exclusive_and_release_discards_state(lab):
    pool = make_pool(lab.controller)
    until(lab, lambda: lab.controller.pool_status(pool)['ready'] == 1)
    first, second = uuid.uuid4().hex, uuid.uuid4().hex
    for lease_id in (first, second):
        pool_call(lab.controller, 'pool_checkout', dict(identity=pool, lease_id=lease_id, owner=None))
    until(lab, lambda: all(lab.controller.state.get('lease', i)['state'] == 'ready' for i in (first, second)))
    ids = [lab.controller.state.get('lease', i)['sandbox'] for i in (first, second)]
    assert len(set(ids)) == 2
    pool_call(lab.controller, 'pool_release', dict(identity=pool, lease_id=first))
    until(lab, lambda: lab.controller.pool_status(pool)['ready'] == 1)
    assert lab.controller.allocation_get(ids[0])['released']
    ready = [a for a in lab.controller.state.list('allocation', parent=pool) if a['state'] == 'ready']
    assert all(a['id'] not in ids for a in ready)


def test_pool_members_inherit_the_prepared_image_definition(lab, monkeypatch):
    snapshots = {}
    original = Executor.call
    def call(self, operation, **params):
        if operation == 'capture':
            saved = copy.deepcopy(self.read(params['identity'])['spec'])
            saved['image'].update(digest='sha256:' + 'a'*64, agent='/private/python',
                                  base_image='images/prepared.erofs')
            saved['template'].update(user='123:456', workdir='/app')
            saved['env']['FROM_IMAGE'] = 'preserved'
            identity = 'snap-' + uuid.uuid4().hex
            snapshots[identity] = saved
            return {'id': identity}
        if operation == 'snapshot_spec':
            return {'reference': params['reference'], 'spec': snapshots[params['reference']]}
        if operation == 'snapshot_info':
            return {'id': params['reference']}
        if operation == 'snapshot_verify':
            return {'status': 'passed'}
        return original(self, operation, **params)
    monkeypatch.setattr(Executor, 'call', call)
    identity = 'pool-image'
    recipe = definition(image='docker://busybox', detached=True, ttl=123)
    pool_call(lab.controller, 'pool_create', dict(identity=identity, name=None, request=recipe,
        size=1, warm=1, weight=1, priority=0, labels={}, placement='spread', owner=None))
    until(lab, lambda: lab.controller.pool_status(identity)['ready'] == 1)
    member = next(a for a in lab.controller.state.list('allocation', parent=identity)
                  if a.get('role') == 'member')
    prepared = member['spec']
    assert prepared['image']['agent'] == '/private/python'
    assert prepared['image']['base_image'] == 'images/prepared.erofs'
    assert prepared['env']['FROM_IMAGE'] == 'preserved'
    assert prepared['template']['user'] == '123:456'
    assert prepared['template']['workdir'] == '/app'
    assert prepared['ttl'] is None
    lease = uuid.uuid4().hex
    pool_call(lab.controller, 'pool_checkout', dict(identity=identity, lease_id=lease, owner=None))
    until(lab, lambda: lab.controller.state.get('lease', lease)['state'] == 'ready')
    worker = lab.executors[member['endpoint']['port']]
    assert worker.read(member['id'])['spec']['ttl'] == 123


def test_controller_restart_preserves_pool_and_active_lease(lab):
    pool = make_pool(lab.controller)
    lease_id = uuid.uuid4().hex
    pool_call(lab.controller, 'pool_checkout', dict(identity=pool, lease_id=lease_id, owner=None))
    until(lab, lambda: lab.controller.state.get('lease', lease_id)['state'] == 'ready')
    sandbox = lab.controller.state.get('lease', lease_id)['sandbox']
    until(lab, lambda: lab.controller.pool_status(pool)['ready'] == 1)
    starts = sum(e.starts for e in lab.executors.values())
    cluster_id = lab.controller.id
    lab.controller.close()
    lab.controller = Controller(lab.directory)
    assert lab.controller.id == cluster_id
    for _ in range(20):
        lab.controller.tick(); time.sleep(.005)
    assert lab.controller.state.get('lease', lease_id)['sandbox'] == sandbox
    assert lab.controller.pool_status(pool)['active'] == 1
    assert sum(e.starts for e in lab.executors.values()) == starts


def test_drain_replaces_idle_members_and_preserves_claimed_episode(lab):
    pool = make_pool(lab.controller)
    lease_id = uuid.uuid4().hex
    pool_call(lab.controller, 'pool_checkout', dict(identity=pool, lease_id=lease_id, owner=None))
    until(lab, lambda: lab.controller.state.get('lease', lease_id)['state'] == 'ready')
    allocation = lab.controller.state.get('allocation', lab.controller.state.get('lease', lease_id)['sandbox'])
    lab.controller.worker_update(allocation['worker'], draining=True)
    for _ in range(10):
        lab.controller.tick(); time.sleep(.005)
    assert lab.controller.allocation_get(allocation['id'])['state'] == 'leased'
    with pytest.raises(ResourceUnavailable):
        lab.controller.worker_update(allocation['worker'], remove=True)


def test_attached_pool_expires_when_creator_exits(lab, monkeypatch):
    from sandweave.sandbox import ownership
    owner = uuid.uuid4().hex
    lab.controller.owner_register(owner, process_identity())
    pool = make_pool(lab.controller, owner=owner)
    until(lab, lambda: lab.controller.pool_status(pool)['ready'] == 1)
    monkeypatch.setattr(ownership, 'process_alive', lambda identity: False)
    until(lab, lambda: lab.controller.pool_status(pool)['state'] == 'closed')
    assert all(a['released'] for a in lab.controller.state.list('allocation', parent=pool))


def test_weighted_admission_charges_each_placement_immediately():
    spec = definition(detached=True)['spec']
    workers = [dict(id='w', state='ready', capacity={'slots': 9, 'memory': 100*1024**3, 'gpu': 0})]
    requests = [dict(id=f'{p}-{i}', parent=p, spec=spec, created=i) for p in ('a', 'b') for i in range(9)]
    assignments, waiting = scheduler.plan(requests, workers, [], {'a': {'weight': 1}, 'b': {'weight': 2}})
    assert sum(i.startswith('a') for i, w, g in assignments) == 3
    assert sum(i.startswith('b') for i, w, g in assignments) == 6
    assert len(waiting) == 9


def test_state_transaction_and_backup(tmp_path):
    state = State(tmp_path / 'state')
    try:
        with pytest.raises(ValueError):
            with state.transaction():
                state.put('request', {'id': 'one', 'files': {'program': b'\x00\xff'}})
                raise ValueError('reject reservation')
        assert state.get('request', 'one', required=False) is None
        state.put('request', {'id': 'one', 'files': {'program': b'\x00\xff'}}, event={'message': 'saved'})
        with pytest.raises(RuntimeError):
            State(tmp_path / 'state')
        state.backup(tmp_path / 'backup.sqlite')
        assert state.events()[0]['detail']['message'] == 'saved'
    finally:
        state.close()
    state = State(tmp_path / 'state')
    try:
        assert state.get('request', 'one')['files']['program'] == b'\x00\xff'
    finally:
        state.close()


def test_gpu_placement_reserves_specific_matching_devices():
    spec = definition(detached=True, gpu=True)['spec']
    worker = dict(id='w', state='ready', capacity={'slots': 4, 'memory': 100*1024**3, 'gpu': 2},
                  gpus=[{'uuid': 'first', 'model': 'L40S'}, {'uuid': 'second', 'model': 'RTX'}])
    requests = [dict(id=str(i), spec=copy.deepcopy(spec), created=i) for i in range(3)]
    requests[0]['spec']['resources']['gpu']['model'] = 'RTX'
    assignments, waiting = scheduler.plan(requests, [worker], [], {})
    assert assignments == [('0', 'w', 'second'), ('1', 'w', 'first')]
    assert '2' in waiting
    allocations = [dict(id='old', worker='w', spec={**spec, '_gpu_uuid': 'second'}, released=False)]
    assignments, waiting = scheduler.plan(requests[:1], [worker], allocations, {})
    assert not assignments
    assert 'GPU' in waiting['0']


def test_unreachable_worker_keeps_charge_and_does_not_replace_episode(lab, monkeypatch):
    identity = request(lab.controller)
    until(lab, lambda: lab.controller.allocation_get(identity)['state'] == 'ready')
    allocation = lab.controller.state.get('allocation', identity)
    failing = allocation['endpoint']['port']
    direct = providers.direct
    def unavailable(endpoint, **kwargs):
        if endpoint['port'] == failing:
            raise OperationUnknown('network partition')
        return direct(endpoint, **kwargs)
    monkeypatch.setattr(providers, 'direct', unavailable)
    lab.controller.connector = lambda *a, **kw: unavailable({'port': failing})
    for _ in range(10):
        lab.controller.next_probe = 0
        lab.controller.tick(); time.sleep(.01)
    assert not lab.controller.allocation_get(identity)['released']
    assert sum(e.starts for e in lab.executors.values()) == 1
    assert lab.controller.state.get('allocation', identity)['worker'] == allocation['worker']


def test_expired_unacknowledged_request_is_cancelled(lab):
    identity = request(lab.controller)
    with lab.controller.state.transaction():
        record = lab.controller.state.get('allocation', identity)
        lab.controller.state.put('allocation', {**record, 'deadline': time.time() - 1})
    until(lab, lambda: lab.controller.allocation_get(identity)['released'])
    assert sum(e.starts for e in lab.executors.values()) == 0


def test_worker_endpoint_refresh_keeps_existing_assignment(lab):
    identity = request(lab.controller)
    until(lab, lambda: lab.controller.allocation_get(identity)['state'] == 'ready')
    original = lab.controller.state.get('allocation', identity)
    index = original['endpoint']['port']
    executor = lab.executors[index]
    executor.index = 10
    information = executor.call('inventory')
    information['cpus'] = [index]
    route = lab.controller._accept_worker(original['worker'], Link(executor), information)
    updated = lab.controller.state.get('allocation', identity)
    assert route['port'] == 10 and updated['endpoint'] == route
    assert updated['generation'] == original['generation']
    executor.index = index


def test_job_retry_waits_for_confirmed_cleanup(lab, monkeypatch):
    from sandweave.weave.jobs import dispatch, _finish, reconcile
    monkeypatch.setattr(lab.controller, '_reconcile_jobs', lambda: None)
    pool = make_pool(lab.controller)
    request_data = dict(command='exit 75', files={}, items=[None], retries=1, retry_codes=[75],
                        retry_infrastructure=False, timeout=None, max_output_bytes=1024, every=None)
    dispatch(lab.controller, 'job_create', dict(identity='job-retry', pool=pool, request=request_data))
    # Drive the lease only; this fake executor intentionally has no guest agent.
    reconcile(lab.controller)
    task = lab.controller.state.list('task')[0]
    until(lab, lambda: lab.controller.state.get('lease', task['lease'])['state'] == 'ready')
    lease = lab.controller.state.get('lease', task['lease'])
    with lab.controller.state.transaction():
        task = lab.controller.state.get('task', task['id'])
        lab.controller.state.put('task', {**task, 'sandbox': lease['sandbox'], 'state': 'running'})
    _finish(lab.controller, task['id'], dict(returncode=75, stdout=b'', stderr=b'', failure='application'))
    with lab.controller.state.transaction():
        current = lab.controller.state.get('task', task['id'])
        lab.controller.state.put('task', {**current, 'due': 0})
    reconcile(lab.controller)
    assert lab.controller.state.get('task', task['id'])['state'] == 'pending'
    assert lab.controller.state.get('task', task['id'])['lease'] == task['lease']


def test_job_submission_is_durable_without_available_workers(tmp_path, monkeypatch):
    from sandweave import Cluster, Job
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'client'))
    cluster = Cluster.start('queued', directory=tmp_path / 'controller', local_worker=False)
    try:
        job = Job.submit('echo saved', target='queued', files={'input': b'durable'}, detached=True)
        assert job.info['state'] == 'running'
        with pytest.raises(TimeoutError):
            job.wait(timeout=.1)
        assert job.info['state'] == 'running'
        assert Job.connect(job.id, target='queued').cancel()['state'] == 'cancelled'
        with pytest.raises(FileExistsError):
            Cluster.start('queued', directory=tmp_path / 'conflict', local_worker=False)
        assert not (tmp_path / 'conflict/controller.json').exists()
    finally:
        cluster.stop()
        cluster.close()


def test_cluster_cache_publication_rejects_concurrent_alias_change(lab, monkeypatch):
    from sandweave.sandbox.errors import CacheConflict
    class SnapshotWorker:
        def call(self, operation, **params):
            return {'reference': params['reference']} if operation == 'snapshot_spec' else {'id': params['reference']}
        def close(self):
            pass
    monkeypatch.setattr(providers, 'direct', lambda *a, **kw: SnapshotWorker())
    first, second = 'snap-' + 'a'*32, 'snap-' + 'b'*32
    artifacts.register(lab.controller, first, {}, key='baseline', expected=None, compare=True)
    with pytest.raises(CacheConflict):
        artifacts.register(lab.controller, second, {}, key='baseline', expected=None, compare=True)
    assert lab.controller.state.get('alias', 'baseline')['reference'] == first


def test_failed_job_pool_does_not_block_other_reconciliation(lab):
    from sandweave.weave.jobs import dispatch, reconcile
    pool = make_pool(lab.controller)
    request_data = dict(command='true', files={}, items=[None], retries=0, retry_codes=[],
                        retry_infrastructure=False, timeout=None, max_output_bytes=1024, every=None)
    dispatch(lab.controller, 'job_create', dict(identity='job-failed-pool', pool=pool, request=request_data))
    with lab.controller.state.transaction():
        record = lab.controller.state.get('pool', pool)
        lab.controller.state.put('pool', {**record, 'state': 'failed', 'error': 'installation failed'})
    reconcile(lab.controller)
    assert dispatch(lab.controller, 'job_status', {'identity': 'job-failed-pool'})['state'] == 'failed'
    assert dispatch(lab.controller, 'job_results', {'identity': 'job-failed-pool'})[0]['error'] == 'installation failed'


def test_artifact_replica_is_used_when_first_worker_cannot_connect(lab, monkeypatch):
    identity = 'snap-' + 'c'*32
    lab.controller.state.put('artifact', dict(id=identity, spec={'reference': identity},
        locations=[{'port': 1}, {'port': 2}], info={}))
    class Replica:
        def call(self, operation, **params):
            assert operation == 'snapshot_info'
            return {'id': identity}
        def close(self):
            pass
    def connection(endpoint):
        if endpoint['port'] == 1:
            raise ResourceUnavailable('SSH forwarding failed')
        return Replica()
    monkeypatch.setattr(providers, 'direct', connection)
    assert artifacts.dispatch(lab.controller, 'snapshot_info', {'reference': identity}) == {'id': identity}


def test_first_use_cluster_target_follows_selected_storage(tmp_path, monkeypatch):
    from sandweave import Cluster
    from sandweave.sandbox import targets
    from sandweave.weave.client import cluster_config
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'before-setup'))
    selected = tmp_path / 'selected-data'
    def prepare(**kwargs):
        monkeypatch.setenv('SANDWEAVE_HOME', str(selected))
        return SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(targets, 'local_connection', prepare)
    monkeypatch.setattr(Cluster, 'add_worker', lambda *a, **kw: None)
    with pytest.raises(ValueError, match='reserved'):
        Cluster.start('local')
    cluster = Cluster.start('first-use')
    try:
        assert cluster_config('first-use') == cluster.config
        assert Path(cluster.config['directory']).is_relative_to(selected)
        assert Cluster.connect('first-use').info['protocol'] == 1
    finally:
        cluster.stop()
        cluster.close()


def test_prepared_worker_installs_missing_control_dependency_without_replacing_it(lab, monkeypatch):
    from sandweave import onboarding, installation
    from sandweave.sandbox import preparation
    worker = lab.executors[1]
    worker.endpoint = SimpleNamespace(connection=lambda: Link(worker))
    monkeypatch.setattr(onboarding, 'python_packages', lambda recipe: [('sandweave_missing_optional_test_dependency', 'test-only')])
    monkeypatch.setattr(onboarding, 'validate_assets', lambda *a, **kw: None)
    monkeypatch.setattr(installation, 'needs_helpers', lambda *a: False)
    installed = []
    monkeypatch.setattr(preparation, 'ensure', lambda recipe: installed.append(recipe['name']))
    recipe = definition()['spec']['template']
    result = worker.management.prepare(recipe)
    assert installed == [recipe['name']]
    assert result['information']['workspace'] == str(worker.root)
    assert worker.starts == 0


def test_remote_attached_job_waits_until_creator_knows_worker_route(lab, monkeypatch):
    from sandweave.weave.jobs import dispatch, reconcile, _step
    monkeypatch.setattr(lab.controller, '_reconcile_jobs', lambda: None)
    owner = uuid.uuid4().hex
    lab.controller.owner_register(owner, None)  # No locally observable PID.
    pool = make_pool(lab.controller, owner=owner)
    request_data = dict(command='true', files={}, items=[None], retries=0, retry_codes=[],
                        retry_infrastructure=False, timeout=None, max_output_bytes=1024, every=None)
    dispatch(lab.controller, 'job_create', dict(identity='job-remote-owner', owner=owner, pool=pool, request=request_data))
    reconcile(lab.controller)
    task = lab.controller.state.list('task')[0]
    until(lab, lambda: lab.controller.state.get('lease', task['lease'])['state'] == 'ready')
    called = []
    class Guest:
        def call(self, operation, **params):
            called.append(operation)
            if operation == 'process_status':
                return {'returncode': 0, 'stdout_size': 0, 'stderr_size': 0}
        def close(self):
            pass
    monkeypatch.setattr(providers, 'direct', lambda *a, **kw: Guest())
    _step(lab.controller, task['id'])
    assert not called
    info = dispatch(lab.controller, 'job_status', {'identity': 'job-remote-owner'})
    route = info['_owner_routes'][0]
    lab.controller.owner_routes(owner, [route['id']])
    _step(lab.controller, task['id'])
    assert called.count('command_start') == 1
    assert lab.controller.state.get('task', task['id'])['state'] == 'succeeded'


def test_connection_reopens_closed_idle_socket_before_sending():
    import select
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from sandweave.sandbox.connection import Connection
    from sandweave.sandbox.wire import encode
    calls = []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            calls.append(1)
            payload = encode({'result': len(calls)})
            self.send_response(200)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            self.close_connection = True
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Connection('127.0.0.1', server.server_port, 'test')
    try:
        assert client.call('mutation') == 1
        assert select.select([client.idle[0].sock], [], [], 2)[0]
        assert client.call('mutation') == 2
        assert len(calls) == 2
    finally:
        client.close()
        server.shutdown(); server.server_close(); thread.join()


def test_artifact_transfer_preserves_files_symlinks_and_rejects_corruption(tmp_path, monkeypatch):
    from sandweave.sandbox.artifacts import Artifacts
    from sandweave.sandbox.snapshots import Store
    from sandweave.sandbox.runtimes.apptainer.driver import inventory, digest
    from sandweave.sandbox.wire import encode
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'home'))
    origin = tmp_path / 'origin'
    origin.mkdir()
    (origin / 'base').write_bytes(b'immutable base')
    snapshot = origin / 'snapshot'
    (snapshot / 'upper').mkdir(parents=True)
    (snapshot / 'upper/a').write_bytes(b'hello\x00world')
    (snapshot / 'upper/link').symlink_to('/workspace/a')
    manifest = dict(backend='apptainer', kind='filesystem', snapshot_id='test',
                    base_image=dict(path='base', size=14, sha256=digest(origin / 'base')),
                    files=inventory(snapshot / 'upper'))
    (snapshot / 'snapshot-manifest.json').write_text(json.dumps(manifest))
    source = SimpleNamespace(root=origin, store=Store(SimpleNamespace(root=origin)))
    identity = 'snap-' + 'b'*32
    saved = dict(id=identity, workspace=str(origin), location=str(snapshot), digest='stable-digest',
                 spec={'runtime': 'apptainer'}, state='filesystem', source='sw-source')
    (source.store.root / 'revisions' / (identity + '.bin')).write_bytes(encode(saved))
    sender = Artifacts(source)
    bundle = sender.manifest(identity)
    destination_root = tmp_path / 'destination'
    destination_root.mkdir()
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'other-home'))
    destination = SimpleNamespace(root=destination_root, store=Store(SimpleNamespace(root=destination_root)))
    receiver = Artifacts(destination)
    missing = receiver.begin(bundle)
    for name in missing:
        size = bundle['files'][name]['size']
        if size:
            receiver.write(identity, name, 0, sender.read(identity, name, 0, size))
    receiver.write(identity, 'snapshot/upper/a', 0, b'wrong')
    with pytest.raises(Exception, match='checksum mismatch'):
        receiver.finish(identity)
    receiver.write(identity, 'snapshot/upper/a', 0, b'hello\x00world')
    receiver.finish(identity)
    imported = destination.store.resolve(identity)
    assert (Path(imported['location']) / 'upper/a').read_bytes() == b'hello\x00world'
    assert (Path(imported['location']) / 'upper/link').readlink() == Path('/workspace/a')
    with pytest.raises(Exception, match='immutable'):
        receiver.write(identity, 'snapshot/upper/a', 0, b'wrong')
    malicious = copy.deepcopy(bundle)
    malicious['files']['snapshot/upper/link/escape'] = {'kind': 'file', 'size': 0, 'sha256': hashlib.sha256(b'').hexdigest()}
    with pytest.raises(ValueError, match='non-directory parent'):
        receiver.begin(malicious)

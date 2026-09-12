"""Retention must reclaim its own payloads without racing other users of a store."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
from pathlib import Path
import socket
import threading
from types import SimpleNamespace

import pytest

from sandweave.sandbox import retention
from sandweave.sandbox.artifacts import Artifacts
from sandweave.sandbox.connection import Connection
from sandweave.sandbox.errors import CacheMiss, ResourceUnavailable
from sandweave.sandbox.snapshots import Store
from sandweave.sandbox.wire import encode
from sandweave.weave import artifacts
from sandweave.weave.controller import Controller
from sandweave.weave.pool import dispatch, _release_artifacts
from sandweave.sandbox.sandbox import definition
from test_weave import Executor, lab, request, until
from test_weave_transport import executor_server

POOL = 'pool-' + 'a'*32
SNAP = 'snap-' + 'b'*32
SOURCE = 'sw-source'


def test_idle_socket_above_select_fd_limit(tmp_path):
    executor = Executor(tmp_path/'executor', 1)
    executor.call = lambda op, **kw: kw['value']
    with executor_server(executor) as port:
        connection = Connection('127.0.0.1', port, 'private-worker-token')
        try:
            assert connection.call('echo', value=1) == 1
            http = connection.idle[0]
            replacement = socket.socket(fileno=fcntl.fcntl(http.sock.fileno(), fcntl.F_DUPFD_CLOEXEC, 2048))
            replacement.settimeout(http.sock.gettimeout())
            http.sock.close()
            http.sock = replacement
            assert connection.call('echo', value=2) == 2
        finally:
            connection.close()


def test_existing_local_snapshot_skips_shared_publication_and_transfer(tmp_path, monkeypatch):
    controller = Controller(tmp_path/'controller')
    endpoint = {'port': 1}
    calls = []
    class Destination:
        def call(self, operation, **params):
            calls.append(operation)
            assert operation == 'snapshot_info'
            return {'id': SNAP}
    try:
        controller.state.put('artifact', {'id': SNAP, 'locations': [], 'info': {'source': SOURCE}})
        def register(c, reference, e):
            calls.append('register')
            assert e == endpoint and reference == SNAP
        monkeypatch.setattr(artifacts, 'register', register)
        assert artifacts.ensure(controller, SNAP, Destination(), endpoint, '/shared/cache') == SNAP
        assert calls == ['snapshot_info', 'register']
        controller.state.put('artifact', {'id': SNAP, 'locations': [endpoint], 'retiring': True})
        with pytest.raises(CacheMiss):
            artifacts.ensure(controller, SNAP, Destination(), endpoint, '/shared/cache')
    finally:
        controller.close()


@pytest.fixture
def stored(tmp_path, monkeypatch):
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path/'home'))
    workers = []
    for i in range(2):
        root = tmp_path / str(i)
        root.mkdir()
        adapter = SimpleNamespace(manager=SimpleNamespace(local=root/'local'))
        runtime = SimpleNamespace(root=root, adapter=lambda name, value=adapter: value)
        worker = SimpleNamespace(root=root, runtime=runtime, store=Store(runtime),
                                 path=lambda identity, r=root: r/'sandboxes'/(identity+'.bin'))
        worker.artifacts = Artifacts(worker)
        for path in (root/'snapshots'/SNAP, root/'local/gvisor/checkpoints'/SNAP,
                     root/'images/pools'/POOL):
            path.mkdir(parents=True)
            (path/'payload').write_text('owned')
        workers.append(worker)
    record = dict(id=SNAP, source=SOURCE, spec={'_retention_pool': POOL, 'runtime': 'gvisor'},
                  workspace=str(workers[0].root), location=str(workers[0].root/'snapshots'/SNAP), digest='test')
    (workers[0].store.root/'revisions'/(SNAP+'.bin')).write_bytes(encode(record))
    return workers, record


def test_shared_store_cleanup_retries_and_preserves_unrelated_files(stored, tmp_path, monkeypatch):
    workers, record = stored
    shared = tmp_path/'shared'
    owned = Path(retention.shared_path(shared, POOL))
    owned.mkdir(parents=True)
    (owned/'payload').write_text('owned')
    unrelated = shared/'keep'
    unrelated.write_text('another pool')
    images = retention.home()/'images/pools'/POOL
    images.mkdir(parents=True)
    (images/'layer').write_text('owned')
    keep = images.parent/'retained-image'
    keep.write_text('another image')
    remove = retention.remove
    failed = []
    def interrupt(path):
        if path == workers[0].root/'images/pools'/POOL and not failed:
            failed.append(True)
            raise OSError('interrupted cleanup')
        remove(path)
    monkeypatch.setattr(retention, 'remove', interrupt)
    with pytest.raises(OSError, match='interrupted'):
        retention.release(workers[0], POOL, [SOURCE], [SNAP], str(shared))
    with pytest.raises(CacheMiss):
        retention.pin(POOL, workers[0].root, 'late-launch')
    for worker in workers:
        assert retention.release(worker, POOL, [SOURCE], shared_cache=str(shared))['released']
        assert not (worker.root/'snapshots'/SNAP).exists()
        assert not (worker.root/'local/gvisor/checkpoints'/SNAP).exists()
        assert not (worker.root/'images/pools'/POOL).exists()
    assert not owned.exists() and not images.exists()
    assert unrelated.read_text() == 'another pool' and keep.read_text() == 'another image'
    assert json.loads(retention.marker(POOL).read_text())['state'] == 'released'
    # A delayed transfer must not resurrect the released snapshot.
    with pytest.raises(CacheMiss):
        workers[0].artifacts.dispatch('artifact_import', {'metadata': record})


def test_live_reference_and_named_cache_block_reclamation(stored):
    workers, record = stored
    first, second = workers
    retention.pin(POOL, second.root, 'external-user')
    with pytest.raises(ResourceUnavailable, match='used by a sandbox'):
        retention.release(first, POOL, [SOURCE], [SNAP])
    assert not retention.marker(POOL).exists()
    retention.unpin(POOL, second.root, 'external-user')
    first.store.publish('keep', record, None)
    with pytest.raises(ResourceUnavailable, match='cache name'):
        retention.release(first, POOL, [SOURCE], [SNAP])
    assert (first.root/'snapshots'/SNAP/'payload').read_text() == 'owned'


def test_snapshot_metadata_still_accepts_cache_names(stored, monkeypatch):
    worker, record = stored[0][0], stored[1]
    worker.store.publish('named-snapshot', record, None)
    monkeypatch.setattr(worker.store, 'verify', lambda record, **kw: {'status': 'passed'})
    assert worker.artifacts.dispatch('artifact_metadata', {'reference': 'named-snapshot'}) == record


def test_release_rejects_relative_shared_path_before_deleting(stored):
    worker = stored[0][0]
    with pytest.raises(ValueError, match='absolute path'):
        retention.release(worker, POOL, [SOURCE], [SNAP], '../other')
    assert (worker.root/'snapshots'/SNAP/'payload').exists()


def test_interrupted_capture_without_revision_record_is_reclaimed(stored):
    workers, _ = stored
    worker = workers[0]
    orphan = 'snap-' + 'c'*32
    retention.capture(POOL, orphan, SOURCE)
    for path in (worker.root/'snapshots'/('.'+orphan+'.publishing'),
                 worker.root/'local/gvisor/checkpoints'/orphan,
                 worker.root/'pool-builds'/POOL/'.image-partial'):
        path.mkdir(parents=True)
        (path/'unfinished').write_text('owned')
    retention.release(worker, POOL, [SOURCE], [SNAP])
    assert not (worker.root/'snapshots'/('.'+orphan+'.publishing')).exists()
    assert not (worker.root/'local/gvisor/checkpoints'/orphan).exists()
    assert not (worker.root/'pool-builds'/POOL).exists()


def test_pin_and_reclamation_are_serialized(stored):
    workers, _ = stored
    worker = workers[0]
    entered = threading.Event()
    def reclaim():
        entered.set()
        return retention.release(worker, POOL, [SOURCE], [SNAP])
    with ThreadPoolExecutor(1) as executor:
        with retention.guard(POOL):
            future = executor.submit(reclaim)
            assert entered.wait(2)
            # This pin is written while holding the same cross-process lock.
            from sandweave.sandbox.workspace import atomic_json
            atomic_json(retention.directory(POOL)/'pins/test.json', {'sandbox': 'racing-user'})
        with pytest.raises(ResourceUnavailable):
            future.result(timeout=3)


def test_pool_retirement_blocks_new_users_and_retries_worker_cleanup(lab, monkeypatch):
    c = lab.controller
    options = dict(identity=POOL, request=definition(detached=True), owner=None,
                   size=1, warm=0, weight=1, priority=0, labels={}, placement='spread', retain_baseline=False)
    dispatch(c, 'pool_create', options)
    c.state.put('pool', {**c.state.get('pool', POOL), 'baseline': SNAP, 'desired': 'closed'})
    endpoint = {'hostname': 'test', 'workspace': 'test', 'port': 1}
    c.state.put('artifact', {'id': SNAP, 'info': {'source': SOURCE}, 'locations': [endpoint]})
    calls = []
    class Releaser:
        def call(self, op, **params):
            calls.append(params)
            if len(calls) == 1:
                raise OSError('worker temporarily unavailable')
            assert op == 'artifact_release' and params['pool'] == POOL
            return {'released': True}
        def close(self): pass
    monkeypatch.setattr(c, 'connection', lambda *a, **kw: Releaser())
    _release_artifacts(c, POOL)
    assert c.state.get('pool', POOL)['cleanup_error']
    with pytest.raises(ResourceUnavailable, match='being released'):
        c.create('late-user', **{**definition(detached=True), 'reference': SNAP})
    _release_artifacts(c, POOL)
    assert c.state.get('pool', POOL)['artifacts_released']
    assert 'cleanup_error' not in c.state.get('pool', POOL)


def test_failed_ephemeral_runtime_is_explicitly_terminated(lab):
    c = lab.controller
    identity = request(c)
    until(lab, lambda: c.allocation_get(identity)['state'] == 'ready')
    record = c.state.get('allocation', identity)
    record['spec']['discard_workspace'] = True
    c.state.put('allocation', record)
    executor = lab.executors[record['endpoint']['port']]
    executor.write({**executor.read(identity), 'state': 'failed'})
    c._observe(identity)
    assert c.state.get('allocation', identity)['released'] is False
    assert c.state.get('allocation', identity)['desired'] == 'terminated'
    until(lab, lambda: c.allocation_get(identity)['released'])
    assert executor.read(identity)['state'] == 'terminated'


def test_unsupported_worker_leaves_no_cleanup_wait(lab):
    c = lab.controller
    dispatch(c, 'pool_create', dict(identity=POOL, request=definition(detached=True), owner=None,
        size=1, warm=0, weight=1, priority=0, labels={}, placement='spread', retain_baseline=False))
    until(lab, lambda: c.state.get('pool', POOL)['state'] == 'failed')
    assert '0.2.13' in c.state.get('pool', POOL)['error']
    dispatch(c, 'pool_close', {'identity': POOL})
    until(lab, lambda: c.state.get('pool', POOL)['state'] == 'closed')
    assert c.state.get('pool', POOL)['artifacts_released']


def test_duplicate_creation_does_not_clean_up_an_existing_sandbox(tmp_path):
    from sandweave.sandbox.worker import Worker
    worker = Worker.__new__(Worker)
    worker.records = tmp_path
    worker.path('existing').write_bytes(b'owned by an earlier operation')
    def reject(*a, **kw):
        raise FileExistsError('different operation')
    worker._prepare_create = reject
    worker.terminate = lambda identity: pytest.fail('terminated someone else\'s sandbox')
    with pytest.raises(FileExistsError):
        worker.create({'_retention_pool': POOL}, 'existing')


@pytest.mark.parametrize('change', [{'cache_key': 'keep'}, {'reference': SNAP},
                                  {'spec': {'runtime': 'apptainer'}}, {'spec': {'keep_on_error': True}}])
def test_retention_rejects_existing_or_explicitly_retained_baselines(lab, change):
    request = definition(detached=True)
    request.update({k: v for k, v in change.items() if k != 'spec'})
    request['spec'].update(change.get('spec', {}))
    with pytest.raises(ValueError, match='new gvisor baseline'):
        dispatch(lab.controller, 'pool_create', dict(identity=POOL, request=request, owner=None,
            size=1, warm=0, weight=1, priority=0, labels={}, placement='spread', retain_baseline=False))

"""Shared immutable storage and pool placement without shared worker state."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import sys
import threading
from types import SimpleNamespace
import uuid

import pytest

from sandweave import Pool
from sandweave.sandbox.artifacts import Artifacts, cache_path
from sandweave.sandbox.errors import CacheMiss, IncompatibleSnapshot
from sandweave.sandbox.sandbox import definition
from sandweave.sandbox.transfers import transfer
from sandweave.sandbox.wire import decode
from sandweave.weave import scheduler
from sandweave.weave.pool import dispatch
from test_weave import lab, make_pool, until


class Link:
    def __init__(self, root, cache):
        root.mkdir()
        store = root / 'store'
        (store / 'revisions').mkdir(parents=True)
        def resolve(reference):
            try:
                return decode((store / 'revisions' / (reference + '.bin')).read_bytes())
            except FileNotFoundError:
                raise CacheMiss(reference)
        self.artifacts = Artifacts(SimpleNamespace(root=root, store=SimpleNamespace(
            root=store, resolve=resolve, verify=lambda record: {'status': 'passed'})))
        self.cache = cache
        self.bytes = 0
        self.fail_read = False

    def call(self, operation, **params):
        if 'shared_cache' in params:
            params['shared_cache'] = str(self.cache)
        if operation == 'artifact_read' and self.fail_read:
            self.fail_read = False
            raise ConnectionError('lost transfer')
        result = self.artifacts.dispatch(operation, params)
        if operation == 'artifact_read':
            self.bytes += len(result)
        return result


@pytest.fixture
def baseline(tmp_path):
    source = Link(tmp_path / 'source', tmp_path / 'source-cache')
    root = source.artifacts.worker.root
    (root / 'snapshot').mkdir()
    (root / 'images').mkdir()
    (root / 'images/base.erofs').write_bytes(b'base image' * 1024)
    (root / 'snapshot/data').write_bytes(b'prepared filesystem' * 1024)
    (root / 'snapshot/snapshot-manifest.json').write_text(json.dumps({'base_image': {'path': 'images/base.erofs'}}))
    reference = 'snap-' + uuid.uuid4().hex
    record = {'id': reference, 'digest': hashlib.sha256(b'baseline').hexdigest(),
              'workspace': str(root), 'location': str(root / 'snapshot'), 'spec': {'runtime': 'apptainer'}}
    source.artifacts._publish(record)
    return source, reference


def test_shared_cache_uses_no_payload_rpc_and_keeps_private_records(baseline, tmp_path):
    source, reference = baseline
    destinations = [Link(tmp_path / str(i), source.cache) for i in range(16)]
    def populate(destination):
        return transfer(source, destination, reference, shared_cache='/worker/cache', lock_root=tmp_path / 'locks')
    with ThreadPoolExecutor(16) as executor:
        assert list(executor.map(populate, destinations)) == [reference] * 16
    assert source.bytes == 0
    records = [d.artifacts.worker.store.resolve(reference) for d in destinations]
    assert len({r['location'] for r in records}) == 1
    assert Path(records[0]['location']).is_relative_to(source.cache)
    assert len({d.artifacts.worker.store.root for d in destinations}) == 16
    assert len(list(source.cache.glob('artifacts-v1/snap-*'))) == 2  # object and lock
    assert (Path(records[0]['location']) / 'data').read_bytes() == b'prepared filesystem' * 1024


def test_node_local_cache_transfers_once_for_sixteen_workers(baseline, tmp_path):
    source, reference = baseline
    destinations = [Link(tmp_path / str(i), tmp_path / 'node-cache') for i in range(16)]
    expected = sum(i['size'] for i in source.artifacts.manifest(reference)['files'].values() if i['kind'] == 'file')
    with ThreadPoolExecutor(16) as executor:
        futures = [executor.submit(transfer, source, d, reference, shared_cache='/same/path', lock_root=tmp_path / 'locks')
                   for d in destinations]
        assert [f.result() for f in futures] == [reference] * 16
    assert source.bytes == expected
    assert all(d.artifacts.worker.store.resolve(reference)['location'].startswith(str(tmp_path / 'node-cache'))
               for d in destinations)


def test_interrupted_shared_transfer_can_resume_and_rejects_changed_content(baseline, tmp_path):
    source, reference = baseline
    destination = Link(tmp_path / 'destination', tmp_path / 'node-cache')
    source.fail_read = True
    with pytest.raises(ConnectionError, match='lost transfer'):
        transfer(source, destination, reference, shared_cache='/cache', lock_root=tmp_path / 'locks')
    assert not destination.call('artifact_cached', reference=reference, shared_cache='/cache')['ready']
    transfer(source, destination, reference, shared_cache='/cache', lock_root=tmp_path / 'locks')
    manifest = source.call('artifact_manifest', reference=reference)
    manifest['metadata']['digest'] = 'different'
    with pytest.raises(IncompatibleSnapshot, match='different manifest'):
        destination.call('artifact_begin', manifest=manifest, shared_cache='/cache')
    with pytest.raises(IncompatibleSnapshot, match='immutable'):
        destination.call('artifact_write', reference=reference, path='snapshot/data', offset=0,
                         data=b'changed', shared_cache='/cache')


def test_cache_identity_uses_storage_not_path_or_hostname(baseline, tmp_path):
    source, reference = baseline
    shared = tmp_path / 'alias'
    shared.symlink_to(source.cache)
    a = Link(tmp_path / 'a', shared)
    b = Link(tmp_path / 'b', tmp_path / 'different-cache')
    original = source.call('artifact_cached', reference=reference, shared_cache='/cache')['cache_id']
    assert a.call('artifact_cached', reference=reference, shared_cache='/cache')['cache_id'] == original
    assert b.call('artifact_cached', reference=reference, shared_cache='/cache')['cache_id'] != original


def test_completed_cache_does_not_contact_an_offline_source_or_rehash(baseline, tmp_path):
    source, reference = baseline
    destination = Link(tmp_path / 'destination', source.cache)
    transfer(source, destination, reference, shared_cache='/cache', lock_root=tmp_path / 'locks')
    def unexpected(*args, **kwargs):
        raise AssertionError('a registered cache hit must not transfer or rehash files')
    source.call = unexpected
    destination.artifacts.worker.store.verify = unexpected
    assert transfer(source, destination, reference, shared_cache='/cache', lock_root=tmp_path / 'locks') == reference


def test_materialized_images_do_not_repeat_cross_filesystem_reads(tmp_path, monkeypatch):
    from sandweave.sandbox import snapshots
    source, worker = tmp_path / 'shared', tmp_path / 'worker'
    (source / 'images').mkdir(parents=True)
    (source / 'tools/runtime').mkdir(parents=True)
    (source / 'snapshot').mkdir()
    (source / 'images/base').write_bytes(b'base image')
    (source / 'tools/runtime/runsc').write_bytes(b'runtime')
    (source / 'snapshot/state').write_bytes(b'saved state')
    (source / 'snapshot/verification.json').write_text('{"status":"passed"}')
    manifest = {'base_image': {'path': 'images/base'}, 'runtime': {'path': 'tools/runtime'}, 'files': {'state': {}}}
    (source / 'snapshot/snapshot-manifest.json').write_text(json.dumps(manifest))
    monkeypatch.setitem(sys.modules, 'snapshot_store', SimpleNamespace(inspect=lambda *args: manifest))
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'state'))
    copies = []
    def copy_file(src, dst):
        copies.append((src, dst))
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    monkeypatch.setattr(snapshots, '_immutable', copy_file)
    store = snapshots.Store(SimpleNamespace(root=worker))
    record = {'id': 'snap-' + uuid.uuid4().hex, 'workspace': str(source), 'location': str(source / 'snapshot')}
    destination = store.materialize(record)
    count = len(copies)
    assert count == 2 and (destination / 'state').read_bytes() == b'saved state'
    assert store.materialize(record) == destination and len(copies) == count
    # A changed destination cannot keep using the remembered file identity.
    (worker / 'images/base').write_bytes(b'wrong file')
    store.materialize(record)
    assert len(copies) > count and (worker / 'images/base').read_bytes() == b'base image'


@pytest.mark.parametrize('path', ['', 'relative', '~/cache', '/cache/../state', '/cache\0bad'])
def test_cache_requires_a_literal_absolute_worker_path(path):
    with pytest.raises(ValueError, match='absolute path'):
        Pool(shared_cache=path)
    assert cache_path(Path('/worker/cache')) == '/worker/cache'


def workers():
    return [dict(id=name, state='ready', inventory={'scope': {'boot': boot, 'pid_namespace': name}},
                 capacity={'slots': 2, 'memory': 16 * 1024**3, 'gpu': 0})
            for name, boot in [('a', 'one'), ('b', 'two'), ('c', 'one')]]


def requests(count, parent='pool'):
    return [dict(id=str(i), parent=parent, spec=definition()['spec'], created=i, desired='running') for i in range(count)]


@pytest.mark.parametrize('affinity,expected', [(None, ['a', 'b', 'c']), ('worker', ['a', 'a', 'b']),
                                             ('machine', ['a', 'c', 'a'])])
def test_affinity_preserves_default_spread_and_spills_when_full(affinity, expected):
    placed, waiting = scheduler.plan(requests(3), workers(), [], {'pool': {'affinity': affinity}})
    assert [w for _, w, _ in placed] == expected and not waiting


def test_machine_affinity_spills_to_other_host_when_local_workers_are_full():
    placed, waiting = scheduler.plan(requests(6), workers(), [], {'pool': {'affinity': 'machine'}})
    assert [w for _, w, _ in placed] == ['a', 'c', 'a', 'c', 'b', 'b'] and not waiting


def test_affinity_retains_builder_location_but_respects_labels_and_draining():
    fleet = workers()
    builder = {**requests(1)[0], 'id': 'builder', 'worker': 'c', 'released': True, 'prepared': True}
    policy = {'pool': {'affinity': 'worker'}}
    placed, _ = scheduler.plan(requests(1), fleet, [builder], policy)
    assert placed[0][1] == 'c'
    fleet[2]['draining'] = True
    fleet[1]['labels'] = {'allowed': 'yes'}
    policy['pool']['labels'] = {'allowed': 'yes'}
    placed, _ = scheduler.plan(requests(1), fleet, [builder], policy)
    assert placed[0][1] == 'b'


def test_affinity_does_not_override_memory_or_gpu_admission():
    fleet = workers()
    builder = {**requests(1)[0], 'id': 'builder', 'worker': 'a', 'released': True, 'prepared': True}
    fleet[0]['capacity']['memory'] = 1
    placed, _ = scheduler.plan(requests(1), fleet, [builder], {'pool': {'affinity': 'worker'}})
    assert placed[0][1] == 'b'
    fleet[1]['capacity']['gpu'] = 1
    fleet[1]['gpus'] = [{'uuid': 'gpu-b', 'model': 'L40S'}]
    request = requests(1)[0]
    request['spec'] = definition(gpu='L40S')['spec']
    placed, _ = scheduler.plan([request], fleet, [builder], {'pool': {'affinity': 'machine'}})
    assert placed == [('0', 'b', 'gpu-b')]


def test_unavailable_machine_identity_never_groups_by_hostname():
    fleet = workers()
    for worker in fleet:
        worker['inventory'] = {'hostname': 'same-name', 'scope': None}
    placed, _ = scheduler.plan(requests(3), fleet, [], {'pool': {'affinity': 'machine'}})
    assert [w for _, w, _ in placed] == ['a', 'a', 'b']
    assert scheduler.machine({'scope': {'boot': 'a', 'pid_namespace': 'first'}}) == scheduler.machine(
        {'scope': {'boot': 'a', 'pid_namespace': 'second'}})


def test_pool_exposes_cache_and_affinity_and_passes_cache_to_launch(lab, monkeypatch):
    assert set(lab.controller.dispatch('ping', {})['pool_options']) >= {'shared_cache', 'affinity'}
    pool = make_pool(lab.controller, size=2, warm=2)
    record = lab.controller.state.get('pool', pool)
    lab.controller.state.put('pool', {**record, 'shared_cache': '/workers/shared', 'affinity': 'machine'})
    calls = []
    monkeypatch.setattr('sandweave.weave.artifacts.ensure', lambda *args: calls.append(args[-1]))
    until(lab, lambda: lab.controller.pool_status(pool)['ready'] == 2)
    info = lab.controller.pool_status(pool)
    assert info['shared_cache'] == '/workers/shared' and info['affinity'] == 'machine'
    assert calls == ['/workers/shared'] * 2
    assert len({w['machine'] for w in lab.controller.worker_list()}) == 1
    dispatch(lab.controller, 'pool_update', {'identity': pool, 'affinity': 'worker'})
    assert lab.controller.pool_status(pool)['affinity'] == 'worker'
    with pytest.raises(ValueError):
        dispatch(lab.controller, 'pool_update', {'identity': pool, 'shared_cache': '/other'})


def test_older_controller_cannot_silently_ignore_requested_pool_options():
    from sandweave import UnsupportedFeature
    from sandweave.weave.pool import ManagedPool
    pool = ManagedPool.__new__(ManagedPool)
    pool.lock, pool.closed, pool.started = threading.RLock(), False, False
    pool.policy = {'shared_cache': '/cache', 'affinity': 'machine'}
    calls = []
    def old_controller(operation, **params):
        calls.append(operation)
        return {'protocol': 1}
    pool.connection = SimpleNamespace(call=old_controller)
    with pytest.raises(UnsupportedFeature, match='0.2.7'):
        pool._declare()
    assert calls == ['ping']  # No pool was created with silently dropped options.

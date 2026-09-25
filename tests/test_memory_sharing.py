"""Opt-in overcommit changes admission, never the guest's allocation ceiling."""
import argparse
import copy
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest

from sandweave import Memory, ResourceUnavailable, Sandbox, UnsupportedFeature
from sandweave.cli import creation, creation_options
from sandweave.sandbox.admission import admit, reservation
from sandweave.sandbox.information import summarize
from sandweave.sandbox.resources import normalize, restore_resources, uses_memory_reservations
from sandweave.sandbox.sandbox import definition
from sandweave.weave.scheduler import plan
from test_weave import lab, until

GiB = 1024**3


def shared():
    return Memory('16GiB', reservation='4GiB', experimental=True)


@pytest.mark.parametrize('fields', [
    {'reservation': '256MiB'}, {'reservation': True, 'experimental': True},
    {'reservation': '0GiB', 'experimental': True},
    {'reservation': '63MiB', 'experimental': True},
    {'reservation': '2GiB', 'experimental': True},
    {'reservation': '256MiB', 'experimental': 1},
    {'reservation': '256MiB', 'experimental': 'true'},
])
def test_invalid_reservation(fields):
    with pytest.raises(ValueError):
        Memory(**fields)


def test_default_wire_contract_and_explicit_opt_in():
    assert normalize()['memory'] == {'guest': '1GiB', 'runtime': '512MiB'}
    assert reservation(definition(memory=Memory('16GiB', experimental=True))['spec']) == 16.5 * GiB
    spec = definition(memory=shared())['spec']
    assert reservation(spec) == 4.5 * GiB
    assert spec['resources']['memory']['guest'] == '16GiB'
    del spec['resources']['memory']['experimental']
    with pytest.raises(ValueError, match='experimental=True'):
        reservation(spec)  # A raw wire request cannot bypass the opt-in.


def test_service_and_import_memory_are_not_discounted():
    spec = definition(memory=shared())['spec']
    spec['services'] = {'db': {'request': definition(memory=Memory('2GiB', '1GiB'))}}
    spec['_image_import_memory'] = GiB
    assert reservation(spec) == 8.5 * GiB
    assert uses_memory_reservations(spec)
    parent = definition()['spec']
    parent['services'] = {'child': {'request': {'spec': spec}}}
    assert uses_memory_reservations(parent)


def test_disk_ram_cap_and_guest_limit_do_not_change(tmp_path):
    from sandweave.sandbox.runtimes.gvisor.driver import Runtime
    runtime = object.__new__(Runtime)
    runtime.root = tmp_path
    runtime.gpu = lambda *a, **kw: None
    memory = Memory('4GiB', disk='16GiB', disk_path=str(tmp_path),
                    reservation='2GiB', experimental=True)
    spec = definition(memory=memory)['spec']
    assert reservation(spec) == 2.5 * GiB
    options = runtime.options(spec)
    assert options[options.index('--memory-mib') + 1] == '20480'
    assert options[options.index('--ram-mib') + 1] == '4096'
    assert options[options.index('--runtime-memory-mib') + 1] == '512'


def test_template_cli_and_info_preserve_both_values(tmp_path):
    template = tmp_path / 'template.toml'
    template.write_text('extends="coding"\n[resources.memory]\nguest="16GiB"\n'
                        'reservation="4GiB"\nexperimental=true\n')
    spec = definition(template=template)['spec']
    parser = argparse.ArgumentParser()
    creation_options(parser)
    args = parser.parse_args(['--memory', '16GiB', '--memory-reservation', '4GiB',
                              '--experimental-memory-sharing'])
    assert creation(args)['memory'] == shared()
    args.experimental_memory_sharing = False
    with pytest.raises(ValueError):
        creation(args)
    info = summarize({'id': 'sample', 'state': 'ready', 'spec': spec})
    assert info['memory'] == normalize(memory=shared())['memory']
    info['memory']['reservation'] = '1GiB'
    assert spec['resources']['memory']['reservation'] == '4GiB'


def test_four_sixteen_gib_guests_on_thirty_two_gib_worker():
    worker = {'id': 'one', 'state': 'ready', 'capacity': {'memory': 32 * GiB, 'slots': 20, 'gpu': 0}}
    def requests(memory, count):
        return [{'id': str(i), 'created': i, 'spec': definition(memory=memory)['spec']}
                for i in range(count)]
    assignments, waiting = plan(requests(shared(), 8), [worker], [], {})
    assert len(assignments) == 7  # 31.5 GiB; the eighth would reserve 36 GiB.
    assert 'reserved' in waiting['7']
    assignments, waiting = plan(requests('16GiB', 4), [worker], [], {})
    assert len(assignments) == 1  # Runtime allowance still counts.
    assert len(waiting) == 3


def test_worker_concurrent_admission_keeps_reservations_bounded(tmp_path):
    records = {}
    worker = SimpleNamespace(records=tmp_path, memory_budget=18 * GiB, read=records.__getitem__)
    guard = threading.Lock()
    spec = definition(memory=shared())['spec']
    def launch(index):
        with guard:  # The real worker holds this guard through record publication.
            try:
                allocation = admit(worker, spec)
            except ResourceUnavailable:
                return False
            identity = str(index)
            records[identity] = {'id': identity, 'state': 'creating', 'spec': spec, 'admission': allocation}
            (tmp_path / (identity + '.bin')).touch()
            return True
    with ThreadPoolExecutor(32) as threads:
        assert sum(threads.map(launch, range(128))) == 4
    assert sum(r['admission']['memory_reserved'] for r in records.values()) == 18 * GiB
    next(iter(records.values()))['state'] = 'terminated'
    assert launch(128)
    assert not launch(129)


def test_restore_inherits_reservation_and_allows_rebinding(monkeypatch):
    saved = definition(memory=shared())['spec']
    original = copy.deepcopy(saved)
    connection = SimpleNamespace(call=lambda *a, **kw: {'reference': 'snap-pinned', 'spec': saved}, close=lambda: None)
    monkeypatch.setattr('sandweave.sandbox.sandbox.connect', lambda *a, **kw: connection)
    inherited = definition(snapshot='saved')['spec']['resources']
    strict = definition(snapshot='saved', memory='16GiB')['spec']['resources']
    changed = definition(snapshot='saved', memory=Memory('16GiB', reservation='8GiB', experimental=True))['spec']['resources']
    assert inherited['memory']['reservation'] == '4GiB'
    assert 'reservation' not in strict['memory']
    assert changed['memory']['reservation'] == '8GiB'
    assert restore_resources(inherited) == restore_resources(strict) == restore_resources(changed)
    assert saved == original


def test_client_rejects_older_endpoint_before_creating(monkeypatch):
    calls = []
    def call(operation, **kwargs):
        calls.append(operation)
        assert operation == 'ping'
        return {}
    connection = SimpleNamespace(call=call, close=lambda: None)
    connection.clone = lambda **kw: connection
    monkeypatch.setattr('sandweave.sandbox.sandbox.connect', lambda *a, **kw: connection)
    with pytest.raises(UnsupportedFeature, match='memory reservations'):
        Sandbox(memory=shared())
    assert calls == ['ping']


def test_pool_rejects_older_controller_before_declaring():
    from sandweave.weave.pool import ManagedPool
    pool = ManagedPool.__new__(ManagedPool)
    pool.lock = threading.RLock()
    pool.closed = pool.started = False
    pool.target = None
    pool.options = {'memory': shared()}
    pool.policy = {}
    calls = []
    def call(operation, **params):
        calls.append(operation)
        return {}
    pool.connection = SimpleNamespace(call=call)
    with pytest.raises(UnsupportedFeature, match='memory reservations'):
        pool._declare()
    assert calls == ['ping']
    assert pool.started is False


def test_controller_releases_assignment_when_worker_is_too_old(lab):
    controller = lab.controller
    request = definition(memory=Memory('2GiB', reservation='256MiB', experimental=True), detached=True)
    controller.create('shared-old-worker', **request, operation_id='operation')
    until(lab, lambda: controller.allocation_get('shared-old-worker')['released'])
    record = controller.allocation_get('shared-old-worker')
    assert record['state'] in ('failed', 'terminated')
    assert 'memory reservations' in str(record.get('error'))
    assert sum(executor.starts for executor in lab.executors.values()) == 0

"""Public network lifecycle, placement and concurrent membership contracts."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import uuid

import pytest

from sandweave.sandbox.networks import Networks, membership
from sandweave.sandbox.sandbox import definition
from sandweave.sandbox.errors import ResourceUnavailable, UnsupportedFeature
from test_service_network import Hub
from test_weave import lab, until


@pytest.fixture
def networks(tmp_path, monkeypatch):
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'home'))
    root = tmp_path / 'worker'
    root.mkdir()
    worker = SimpleNamespace(root=root, memory_budget=2**30, metadata_path=tmp_path / 'worker.json')
    hub = Hub(tmp_path / 'hub')
    manager = Networks(worker, hub)
    yield manager
    hub.close()


def test_concurrent_memberships_cleanup_and_recovery(networks):
    identity = 'net-' + uuid.uuid4().hex
    assert networks.dispatch('service_network_create', identity)['id'] == identity
    def join(index):
        entry = {'identity': 'sw-' + str(index), 'socket': str(networks.root / f'{index}.sock'),
                 'networks': ['private'], 'aliases': ['member-' + str(index)]}
        return networks.dispatch('service_network_join', identity, entry=entry)
    with ThreadPoolExecutor(16) as executor:
        entries = list(executor.map(join, range(64)))
    assert len({entry['address'] for entry in entries}) == 64
    assert join(0) == entries[0]
    with pytest.raises(ResourceUnavailable, match='terminate'):
        networks.dispatch('service_network_delete', identity)
    networks.hub.close()
    networks.hub = Hub(networks.hub.directory)
    recovered = Networks(networks.worker, networks.hub)
    try:
        assert recovered.dispatch('service_network_info', identity)['members'] == list(networks.read(identity)['members'])
        assert len(recovered.hub.groups[identity]) == 64
        for entry in entries:
            recovered.dispatch('service_network_leave', identity, member=entry['identity'])
        assert not recovered.hub.groups[identity]
        assert recovered.dispatch('service_network_delete', identity)['state'] == 'deleted'
        assert recovered.dispatch('service_network_delete', identity)['state'] == 'deleted'
        with pytest.raises(FileNotFoundError):
            recovered.dispatch('service_network_create', identity)
    finally:
        networks.hub.close()


def test_alias_conflict_rolls_back_and_disjoint_names_are_allowed(networks):
    identity = 'net-' + uuid.uuid4().hex
    networks.dispatch('service_network_create', identity)
    def join(index, scopes):
        return networks.dispatch('service_network_join', identity, entry={
            'identity': f'sw-{index}', 'socket': str(networks.root / f'{index}.sock'),
            'networks': scopes, 'aliases': ['db']})
    join(0, ['one'])
    with pytest.raises(ValueError, match='alias'):
        join(1, ['one'])
    assert len(networks.read(identity)['members']) == 1
    join(1, ['two'])
    assert len(networks.read(identity)['members']) == 2


def test_address_rotation_reuses_free_addresses_without_stealing_live_members(networks):
    from sandweave.sandbox.workspace import atomic_json
    identity = 'net-' + uuid.uuid4().hex
    networks.dispatch('service_network_create', identity)
    def join(index):
        return networks.dispatch('service_network_join', identity, entry={
            'identity': f'sw-{index}', 'socket': str(networks.root / f'{index}.sock'),
            'networks': ['default'], 'aliases': []})
    first = join(0)
    atomic_json(networks.path(identity), {**networks.read(identity), 'next_address': 65535})
    second = join(1)
    assert first['address'] == '10.231.0.2'
    assert second['address'] == '10.231.0.3'


def test_template_workspace_identity_does_not_depend_on_available_ram(networks, monkeypatch, tmp_path):
    root = tmp_path / 'other-template'
    root.mkdir()
    worker = SimpleNamespace(root=root, memory_budget=2**29)
    hub = Hub(tmp_path / 'other-hub')
    try:
        other = Networks(worker, hub)
        assert networks.scope == other.scope
        monkeypatch.setenv('SANDWEAVE_MEMORY_BUDGET', '1GiB')
        assert networks.scope != Networks(worker, hub).scope
    finally:
        hub.close()


def test_membership_is_orthogonal_to_template_and_egress():
    identity = 'net-' + uuid.uuid4().hex
    for template in ('coding', 'gnome', {'extends': 'gnome', 'name': 'custom-desktop'}):
        spec = definition(template=template, service_network=identity, aliases=['DESKTOP'],
                          networks=['private'], network='offline')['spec']
        assert spec['service_network'] == {'id': identity, 'aliases': ['desktop'], 'networks': ['private']}
        assert spec['resources']['network']['mode'] == 'offline'
        if template != 'coding':
            assert 'desktop' in spec['template']['capabilities']
    with pytest.raises(ValueError):
        definition(aliases=['orphan'])
    with pytest.raises(ValueError):
        membership(identity, 'string-not-a-list')
    with pytest.raises(ValueError):
        membership(identity, networks=[])
    with pytest.raises(UnsupportedFeature):
        definition(runtime='apptainer', service_network=identity)


def test_network_members_wait_on_their_worker_instead_of_spilling(lab):
    controller = lab.controller
    identity = 'net-' + uuid.uuid4().hex
    selected = lab.workers[0]['id']
    controller.state.put('network', {'id': identity, 'state': 'ready', 'worker': selected,
        'endpoint': controller.state.get('worker', selected)['endpoint']})
    for worker in lab.executors.values():
        original = worker.call
        def call(operation, _original=original, **params):
            result = _original(operation, **params)
            return {**result, 'service_networks': 1} if operation == 'ping' else result
        worker.call = call
    ids = ['sw-network-a', 'sw-network-b']
    for sandbox in ids:
        controller.create(sandbox, operation_id=uuid.uuid4().hex,
            **definition(detached=True, service_network=identity))
    until(lab, lambda: controller.allocation_get(ids[0])['state'] == 'ready')
    assert controller.allocation_get(ids[0])['worker'] == selected
    assert controller.allocation_get(ids[1])['state'] == 'pending'
    assert 'reserved' in controller.allocation_get(ids[1])['reason']
    controller.allocation_cancel(ids[0])
    until(lab, lambda: controller.allocation_get(ids[1])['state'] == 'ready')
    assert controller.allocation_get(ids[1])['worker'] == selected


def test_worker_rejection_leaves_a_retryable_creation_record(lab):
    from sandweave.weave.networks import dispatch
    identity = 'net-' + uuid.uuid4().hex
    # These legacy fake workers deliberately do not advertise the capability.
    with pytest.raises(UnsupportedFeature):
        dispatch(lab.controller, 'service_network_create', identity)
    assert lab.controller.state.get('network', identity)['state'] == 'failed'
    with pytest.raises(UnsupportedFeature):
        dispatch(lab.controller, 'service_network_create', identity)


def test_lost_worker_cannot_spill_members_and_network_delete_never_contacts_it(lab, monkeypatch):
    from sandweave.weave.networks import dispatch
    controller = lab.controller
    identity = 'net-' + uuid.uuid4().hex
    worker = controller.state.get('worker', lab.workers[0]['id'])
    controller.state.put('network', {'id': identity, 'state': 'ready', 'worker': worker['id'],
                                   'endpoint': worker['endpoint']})
    controller.state.put('worker', {**worker, 'lost': True})
    with pytest.raises(ResourceUnavailable, match='lost'):
        controller.create('sw-no-spill', **definition(detached=True, service_network=identity))
    monkeypatch.setattr(controller, 'connection', lambda *args: pytest.fail('contacted a lost worker'))
    assert dispatch(controller, 'service_network_delete', identity)['state'] == 'deleted'


def test_filesystem_metadata_does_not_retain_the_sources_private_route(tmp_path, monkeypatch):
    from sandweave.sandbox.snapshots import Store
    from sandweave.sandbox.workspace import atomic_json
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'home'))
    identity = 'net-' + uuid.uuid4().hex
    spec = definition(service_network=identity)['spec']
    spec['_service_network'] = {'socket': '/private/old-member', 'address': '10.231.0.2'}
    snapshot = tmp_path / 'snapshot'
    atomic_json(snapshot / 'snapshot-manifest.json', {'snapshot_id': 'native-snapshot'})
    store = Store(SimpleNamespace(root=tmp_path))
    saved = store._record('snap-network-test', {'snapshot': str(snapshot), 'kind': 'filesystem'},
                         {'id': 'sw-source', 'spec': spec, 'agent': None})
    assert '_service_network' not in saved['spec']
    assert '_service_network' in spec
    assert saved['spec']['service_network']['id'] == identity

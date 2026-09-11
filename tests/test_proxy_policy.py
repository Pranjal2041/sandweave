"""Proxy eligibility, distribution and durable pool assignments."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json

import pytest

from sandweave import Network, ProxyPolicy
from sandweave.sandbox import proxy
from sandweave.sandbox.resources import normalize
from sandweave.sandbox.sandbox import definition
from sandweave.sandbox.wire import decode, encode
from sandweave.weave.controller import Controller
from sandweave.weave.pool import dispatch, _new_member
from test_weave import lab, until, Executor


URLS = {'uk': ['http://a:private@1.1.1.1:80', 'http://b:private@8.8.8.8:80'],
        'us': ['http://c:private@9.9.9.9:80']}


def network(distribution='random', region=None):
    return normalize(network=Network(proxy=URLS, policy=ProxyPolicy(distribution, region)))['network']


def test_policy_roundtrip_and_catalog_input_copy():
    urls = copy.deepcopy(URLS)
    value = Network(proxy=urls, policy=ProxyPolicy('round_robin', region='uk'))
    urls['uk'].clear()
    assert len(value.proxy['uk']) == 2
    serialized = normalize(network=value)
    assert normalize(network=decode(encode(serialized))['network']) == serialized
    assert 'private' not in repr(value)
    visible = proxy.public_resources(serialized)
    assert 'private' not in json.dumps(visible)
    assert visible['network']['policy'] == {'distribution': 'round_robin', 'region': 'uk'}
    assert normalize(network=Network(proxy='http://1.1.1.1'))['network'] == {
        'mode': 'proxy', 'allow_cidrs': (), 'proxy': ('http://1.1.1.1',)}


@pytest.mark.parametrize('distribution', ['random', 'round_robin', 'same_proxy', 'same_region'])
def test_standalone_selects_one_random_proxy_in_requested_region(distribution, monkeypatch):
    monkeypatch.setattr(proxy.secrets, 'randbelow', lambda n: n-1)
    selection, _ = proxy.select(network(distribution, 'uk'))
    assert selection['index'] == 1


def test_same_region_selects_uniformly_between_regions_then_cycles(monkeypatch):
    draws = []
    def choose(n):
        draws.append(n)
        return 0
    monkeypatch.setattr(proxy.secrets, 'randbelow', choose)
    net = network('same_region')
    state = {}
    actual = []
    for _ in range(5):
        assigned, state = proxy.select(net, state)
        actual.append(assigned['index'])
    assert actual == [0, 1, 0, 1, 0]
    assert draws == [2]  # Regions, not proxies: no bias toward the larger region.
    assert state == {'region': 'uk', 'cursor': 5}


def test_same_proxy_is_chosen_once_and_state_is_not_mutated(monkeypatch):
    monkeypatch.setattr(proxy.secrets, 'randbelow', lambda n: n-1)
    original = {}
    chosen, state = proxy.select(network('same_proxy'), original)
    assert original == {} and chosen == {'index': 2}
    monkeypatch.setattr(proxy.secrets, 'randbelow', lambda _: pytest.fail('selection must remain fixed'))
    assert proxy.select(network('same_proxy'), state) == (chosen, state)


@pytest.mark.parametrize('kwargs', [
    {'proxy': URLS, 'policy': ProxyPolicy(region='missing')},
    {'proxy': ['http://1.1.1.1'], 'policy': ProxyPolicy(region='uk')},
    {'proxy': ['http://1.1.1.1'], 'policy': ProxyPolicy('same_region')},
    {'proxy': {'uk': []}}, {'proxy': {'': ['http://1.1.1.1']}},
    {'policy': ProxyPolicy()}, {'proxy': URLS, 'policy': 'same_region'},
])
def test_invalid_policy_or_catalog_is_rejected_before_launch(kwargs):
    with pytest.raises(ValueError):
        Network(**kwargs)


def test_group_order_is_stable_across_canonical_encoding():
    original = {'us': URLS['us'], 'uk': URLS['uk']}
    assert proxy.entries(original) == proxy.entries(decode(encode(original, canonical=True)))


def pool_request(controller, net, *, warm=0, size=64):
    request = definition(network=net, detached=True)
    # Register an immutable filesystem baseline, without running guest processes.
    request['reference'] = 'snap-' + 'b'*32
    controller.state.put('artifact', {'id': request['reference'], 'info': {'state': 'filesystem'},
                                     'spec': request, 'locations': []})
    identity = 'pool-policy'
    dispatch(controller, 'pool_create', dict(identity=identity, request=request, name=None,
        owner=None, size=size, warm=warm, weight=1, priority=0, labels={}, placement='spread'))
    return identity


def test_concurrent_assignment_cursor_survives_controller_restart_and_rollback(lab, monkeypatch):
    controller = lab.controller
    identity = pool_request(controller, Network(proxy=URLS, policy=ProxyPolicy('round_robin', region='uk')))
    stale = controller.state.get('pool', identity)
    with ThreadPoolExecutor(16) as executor:
        list(executor.map(lambda _: _new_member(controller, stale), range(64)))
    assignments = controller.state.list('allocation', parent=identity)
    indices = [a['spec']['_proxy_assignment']['index'] for a in assignments]
    assert indices == [0, 1] * 32
    assert controller.state.get('pool', identity)['proxy_state']['cursor'] == 64
    previous = copy.deepcopy(assignments)
    original_create = controller.create
    def broken_create(*args, **kwargs):
        raise OSError('injected failure during the assignment transaction')
    monkeypatch.setattr(controller, 'create', broken_create)
    with pytest.raises(OSError, match='injected'):
        _new_member(controller, stale)
    assert controller.state.get('pool', identity)['proxy_state']['cursor'] == 64
    assert controller.state.list('allocation', parent=identity) == previous
    monkeypatch.setattr(controller, 'create', original_create)
    controller.close()
    lab.controller = Controller(lab.directory, connector=controller.connector)
    assert lab.controller.state.list('allocation', parent=identity) == previous
    _new_member(lab.controller, stale)
    latest = lab.controller.state.list('allocation', parent=identity)[-1]
    assert latest['spec']['_proxy_assignment'] == {'index': 0}
    assert lab.controller.state.get('pool', identity)['proxy_state']['cursor'] == 65


def test_worker_retry_and_claim_keep_exact_pool_assignment(lab, monkeypatch):
    original = Executor.call
    def call(self, op, **kwargs):
        result = original(self, op, **kwargs)
        if op == 'ping':
            result['proxy_policy'] = 1
        return result
    monkeypatch.setattr(Executor, 'call', call)
    for worker in lab.executors.values():
        worker.drop_reply = True
    identity = pool_request(lab.controller, Network(proxy=URLS,
        policy=ProxyPolicy('round_robin', region='uk')), warm=2, size=2)
    until(lab, lambda: lab.controller.pool_status(identity)['ready'] == 2)
    assert sum(w.starts for w in lab.executors.values()) == 2
    records = lab.controller.state.list('allocation', parent=identity)
    assert {a['worker'] for a in records} == {w['id'] for w in lab.workers}
    assert {a['spec']['_proxy_assignment']['index'] for a in records} == {0, 1}
    dispatch(lab.controller, 'pool_checkout', {'identity': identity, 'lease_id': 'lease', 'owner': None})
    until(lab, lambda: lab.controller.state.get('lease', 'lease')['state'] == 'ready')
    for a in records:
        worker = lab.executors[lab.controller.state.get('worker', a['worker'])['endpoint']['port']]
        assert worker.read(a['id'])['spec']['_proxy_assignment'] == a['spec']['_proxy_assignment']
    assert lab.controller.state.get('pool', identity)['proxy_state']['cursor'] == 2


def test_pool_refuses_worker_that_would_ignore_policy(lab):
    identity = pool_request(lab.controller, Network(proxy=URLS, policy=ProxyPolicy('same_proxy')), warm=1, size=1)
    until(lab, lambda: lab.controller.pool_status(identity)['state'] == 'failed')
    assert '0.2.10' in lab.controller.pool_status(identity)['error']
    assert sum(w.starts for w in lab.executors.values()) == 0


def test_builder_establishes_selection_without_consuming_rotation(lab, monkeypatch):
    monkeypatch.setattr(proxy.secrets, 'randbelow', lambda _: 0)
    identity = pool_request(lab.controller, Network(proxy=URLS, policy=ProxyPolicy('same_region')))
    stale = lab.controller.state.get('pool', identity)
    _new_member(lab.controller, stale, builder=True)
    _new_member(lab.controller, stale)
    _new_member(lab.controller, stale)
    allocations = lab.controller.state.list('allocation', parent=identity)
    assert [a['spec']['_proxy_assignment']['index'] for a in allocations] == [0, 0, 1]
    assert lab.controller.state.get('pool', identity)['proxy_state'] == {'region': 'uk', 'cursor': 2}


def test_cli_passes_region_and_distribution_together(tmp_path):
    from sandweave.cli import parser, creation
    file = tmp_path / 'proxies.json'
    file.write_text(json.dumps(URLS))
    args = parser().parse_args(['pool', 'create', '--name', 'test', '--proxy-file', str(file),
                               '--proxy-policy', 'round_robin', '--proxy-region', 'uk'])
    assert creation(args)['network'] == Network(proxy=URLS, policy=ProxyPolicy('round_robin', 'uk'))
    args = parser().parse_args(['create', '--proxy-region', 'uk'])
    with pytest.raises(ValueError, match='require --proxy-file'):
        creation(args)

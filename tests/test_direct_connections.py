"""Direct data traffic over real sockets, retaining Weave lifecycle ownership."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from sandweave import Cluster, Pool, Sandbox
from sandweave.sandbox.errors import OperationUnknown, ResourceUnavailable
from sandweave.weave import providers
from sandweave.weave.worker import Bridge
from test_weave import Executor
from test_weave_transport import executor_server


@pytest.fixture
def cluster(tmp_path, monkeypatch):
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'client'))
    controller = Cluster.start('direct', directory=tmp_path / 'controller',
                               local_worker=False, listen='127.0.0.1:0')
    remote = Cluster.connect(controller.info['connection']['address'],
                             token_file=tmp_path / 'controller/credentials.json')
    executor = Executor(tmp_path / 'worker', 1)
    original = executor.call
    def call(operation, **params):
        if operation == 'control':
            return params['parameters']
        return original(operation, **params)
    executor.call = call
    with executor_server(executor) as port:
        channel = 'd' * 32
        bridge = Bridge(remote.config, channel).start()
        try:
            remote.add_worker({'endpoint': dict(hostname=socket.gethostname(), port=port,
                token='private-worker-token', relay=channel)}, slots=32)
            yield remote
        finally:
            remote.stop()
            bridge.close()
            remote.close()
            controller.close()


def no_controller(*args, **kwargs):
    pytest.fail('data traffic reached the controller')


def test_direct_sync_async_and_scope_bypass_controller(cluster, monkeypatch):
    with Sandbox(target=cluster, connection='direct', detached=True) as env, \
         Sandbox(target=cluster, detached=True) as other:
        connection = env._connection
        clone = connection.clone()
        assert clone.connection_mode == 'direct'
        clone.close()
        worker = connection._worker(connection._route(env.id))
        assert worker.token.startswith('sw1.') and worker.token != 'private-worker-token'
        with pytest.raises(PermissionError):
            worker.call('describe', identity=other.id)
        with pytest.raises(PermissionError):
            worker.call('inventory')
        with monkeypatch.context() as patch:
            patch.setattr(connection.control, 'call', no_controller)
            patch.setattr(connection.control, 'acall', no_controller)
            def step(index):
                return env._call('control', name='desktop', method='step', parameters={'sequence': index})
            with ThreadPoolExecutor(max_workers=16) as clients:
                assert list(clients.map(step, range(128))) == [{'sequence': i} for i in range(128)]
            async def concurrent():
                results = await asyncio.gather(*(env._acall('control', name='desktop', method='action',
                    parameters={'sequence': i}) for i in range(128)))
                assert results == [{'sequence': i} for i in range(128)]
                await connection.aclose()
            asyncio.run(concurrent())
        assert 'connection' not in env.spec
        # Default handles still forward, even with a cached direct route.
        with Sandbox.connect(env.id, target=cluster) as forwarded:
            from sandweave.weave.transport import ForwardedConnection
            assert isinstance(forwarded._connection._worker(forwarded._connection._route(env.id)), ForwardedConnection)
    deadline = time.monotonic() + 5
    while any(not allocation['released'] for allocation in cluster.info['sandboxes']):
        assert time.monotonic() < deadline
        time.sleep(.02)


def test_unreachable_direct_route_cancels_creation_without_fallback(cluster, monkeypatch):
    from sandweave.weave.client import ClusterConnection
    operations = []
    original = ClusterConnection._rpc
    def record(self, operation, **params):
        operations.append(operation)
        return original(self, operation, **params)
    monkeypatch.setattr(ClusterConnection, '_rpc', record)
    def unreachable(*args, **kwargs):
        raise ResourceUnavailable('SSH endpoint unreachable')
    monkeypatch.setattr(providers, 'direct', unreachable)
    with pytest.raises(ResourceUnavailable, match='Direct sandbox connection failed'):
        Sandbox(target=cluster, connection='direct', detached=True)
    assert 'allocation_ack' not in operations
    deadline = time.monotonic() + 5
    while any(not a['released'] for a in cluster.info['sandboxes']):
        assert time.monotonic() < deadline
        time.sleep(.02)


def test_uncertain_direct_action_is_not_replayed(cluster, monkeypatch):
    with Sandbox(target=cluster, connection='direct', detached=True) as env:
        worker = env._connection._worker(env._connection._route(env.id))
        calls = []
        def lost_ack(operation, **kwargs):
            calls.append(operation)
            raise OperationUnknown('reply lost after action')
        monkeypatch.setattr(worker, 'call', lost_ack)
        with pytest.raises(OperationUnknown):
            env._call('control', name='desktop', method='action', parameters={})
        assert calls == ['control']
        assert env.id not in env._connection.registry['routes']
        assert not env._connection.connections
        assert env.info['id'] == env.id  # The next request resolves its route anew.


def test_ssh_endpoint_preserves_registered_login_and_port(monkeypatch):
    endpoint = providers.endpoint({'hostname': 'worker', 'port': 8766}, SimpleNamespace(token='scoped'),
        {'endpoint': {'relay': 'outbound', 'ssh_host': 'alice@worker.example', 'ssh_port': 2222}})
    calls = []
    monkeypatch.setattr(providers, '_tunnel', lambda *a, **kw: calls.append((a, kw)) or 54321)
    connection = providers.direct(endpoint)
    assert calls == [(('alice@worker.example', 8766), {'ssh_port': 2222})]
    assert (connection.host, connection.port, connection.token) == ('127.0.0.1', 54321, 'scoped')
    connection.close()


def test_pool_mode_is_client_only_and_follows_lease(monkeypatch):
    from sandweave.weave import pool as module
    from sandweave.sandbox.sandbox import definition
    config = {'url': 'http://127.0.0.1:1', 'token': 'test'}
    calls, releases = [], []
    class Link:
        def __init__(self, config, **options):
            calls.append(options)
        def call(self, operation, **kwargs):
            if operation == 'pool_lease':
                return {'state': 'ready', 'sandbox': 'sw-test', 'route': {'id': 'sw-test'}}
            if operation == 'pool_status':
                return {'id': 'pool-test', 'size': 1, 'warm': 0}
        def remember(self, route):
            pass
        def forget(self, identity):
            releases.append(identity)
    monkeypatch.setattr('sandweave.weave.client.ClusterConnection', Link)
    monkeypatch.setattr(module, 'client_owner', lambda c: 'owner')
    pool = Pool(target={'cluster': config}, connection='direct')
    assert calls == [{'connection': 'direct'}]
    assert 'connection' not in pool.options
    assert 'connection' not in definition(**pool.options)['spec']
    monkeypatch.setattr(pool, 'start', lambda: pool)
    monkeypatch.setattr(pool, '_release_lease', lambda identity: releases.append('released'))
    def connect(identity, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(id=identity, close=lambda: releases.append('closed'))
    monkeypatch.setattr(Sandbox, 'connect', connect)
    with pool.acquire() as env:
        assert env.id == 'sw-test'
        assert calls[-1] == {'target': {'cluster': config}, 'connection': 'direct'}
    assert releases == ['released', 'sw-test', 'closed']
    attached = Pool.connect('pool-test', target={'cluster': config}, connection='direct')
    assert attached.connection_mode == 'direct'


def test_pool_failed_direct_attach_releases_lease(monkeypatch):
    from sandweave.weave import pool as module
    pool = object.__new__(module.ManagedPool)
    pool.wait_timeout, pool.target, pool.id, pool.connection_mode = 1, 'lab', 'pool-test', 'direct'
    pool.connection = SimpleNamespace(call=lambda op, **kw: {'state': 'ready', 'route': {}, 'sandbox': 'sw-test'},
                                      remember=lambda route: None)
    pool.start = lambda: pool
    released = []
    pool._release_lease = released.append
    monkeypatch.setattr(module, 'client_owner', lambda c: 'owner')
    def unavailable(*args, **kwargs):
        raise ResourceUnavailable('direct unavailable')
    monkeypatch.setattr(Sandbox, 'connect', unavailable)
    with pytest.raises(ResourceUnavailable), pool.acquire():
        pytest.fail('unreachable lease was delivered')
    assert len(released) == 1


@pytest.mark.parametrize('value', ['auto', '', None, 1])
def test_invalid_mode_rejected_before_installation(value):
    with pytest.raises(ValueError, match='connection must be'):
        Sandbox(connection=value)
    with pytest.raises(ValueError, match='connection must be'):
        Sandbox.connect('test', connection=value)
    with pytest.raises(ValueError, match='connection must be'):
        Pool(connection=value)


def test_cli_direct_options():
    from sandweave.cli import creation, parser
    args = parser().parse_args(['run', '--target', 'lab', '--connection', 'direct', '--', 'true'])
    assert creation(args)['connection'] == 'direct'
    assert parser().parse_args(['desktop', 'step', 'sw-test', '{}', '--output', 'frame.png',
                               '--connection', 'direct']).connection == 'direct'


def test_cancelled_async_connection_setup_drains_without_blocking_loop(monkeypatch):
    from sandweave.weave.client import ClusterConnection
    connection = ClusterConnection({'url': 'http://127.0.0.1:1', 'token': 'test'}, connection='direct')
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def setup(route):
        entered.set()
        assert release.wait(5)
        finished.set()
        return object()
    monkeypatch.setattr(connection, '_worker', setup)
    async def run():
        task = asyncio.create_task(connection._aworker({'id': 'sw-test', 'endpoint': {}}))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(.02)
            assert not task.done() and not finished.is_set()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
    try:
        asyncio.run(run())
    finally:
        release.set()
        connection.close()


def test_direct_vnc_stream_bypasses_controller(tmp_path, monkeypatch):
    from test_vnc_streams import worker_server, BANNER, exact
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'client'))
    cluster = Cluster.start('streams-direct', directory=tmp_path / 'controller', local_worker=False)
    try:
        with worker_server(tmp_path / 'worker') as (worker, target):
            cluster.add_worker({'endpoint': dict(hostname=socket.gethostname(), port=target.port,
                                                 token=target.token)}, slots=2)
            with Sandbox(target=cluster, connection='direct', template='gnome', cpu=1, detached=True) as env:
                async def forbidden(*args, **kwargs):
                    pytest.fail('VNC stream reached the controller')
                monkeypatch.setattr(env._connection.control, 'open_stream', forbidden)
                stream = env.desktop.vnc()
                try:
                    assert exact(stream, len(BANNER)) == BANNER
                    assert stream.write(b'\x00direct\xff') == 8
                    assert exact(stream, 8) == b'\x00direct\xff'
                finally:
                    stream.close()
                async def run():
                    stream = await env.desktop.vnc.aio()
                    try:
                        assert await stream.read.aio(12) == BANNER
                    finally:
                        await stream.close.aio()
                        await env._connection.aclose()
                asyncio.run(run())
    finally:
        cluster.stop()
        cluster.close()

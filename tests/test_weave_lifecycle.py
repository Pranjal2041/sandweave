"""Lifecycle concurrency and read isolation using real HTTP/relay RPCs."""
import asyncio
from collections import Counter
from contextlib import asynccontextmanager
import json
import socket
import threading
import time

from aiohttp import web
import pytest

from sandweave.sandbox.async_connection import AsyncConnection
from sandweave.sandbox.sandbox import definition
from sandweave.sandbox.wire import decode, encode
from sandweave.weave.controller import Controller
from sandweave.weave.dashboard import Dashboard
from sandweave.weave.pool import _claim, dispatch
from sandweave.weave.server import RPC


@asynccontextmanager
async def listener(handler):
    app = web.Application()
    app.router.add_route('*', '/{path:.*}', handler)
    runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0, backlog=4096)
    await site.start()
    try:
        yield site._server.sockets[0].getsockname()[1]
    finally:
        await runner.cleanup()


def seed(controller, endpoint, count, action):
    """Start at a durable lifecycle boundary, without launching fake guests."""
    request = definition(detached=True)
    with controller.state.transaction():
        controller.state.put('pool', dict(id='pool-test', name='named-pool', state='ready', desired='running',
            request=request, size=count, warm=0, retain_baseline=True))
        controller.state.put('worker', dict(id='worker-test', endpoint=endpoint))
        for i in range(count):
            identity = 'sw-' + str(i)
            claiming = action == 'claim'
            controller.state.put('allocation', dict(id=identity, parent='pool-test', worker='worker-test',
                state='claiming' if claiming else 'reserved' if action == 'create' else 'ready',
                desired='terminated' if action == 'terminate' else 'running', released=False,
                generation=2 if claiming else 1, lease='lease-' + str(i) if claiming else None,
                role='member', endpoint=endpoint, token='sandbox-token', spec=request['spec'], request=request))
            controller.state.put('lease', dict(id='lease-' + str(i), parent='pool-test', owner=None,
                state='claiming' if claiming else 'pending', sandbox=identity if claiming else None,
                generation=2 if claiming else 1))


@pytest.mark.parametrize('action', ['create', 'claim', 'terminate'])
@pytest.mark.parametrize('relay', [False, True])
def test_256_lifecycle_waits_leave_threads_free_and_serialize_allocations(tmp_path, action, relay):
    async def run():
        controller = Controller(tmp_path / 'controller')
        release, all_entered = asyncio.Event(), asyncio.Event()
        entered, calls = set(), Counter()
        count = 256
        async def work(operation, parameters):
            if operation == 'managed_apply':
                identity = parameters['identity']
                calls[(identity, parameters['action'], parameters['generation'])] += 1
                if identity.startswith('sw-') and parameters['action'] == action:
                    entered.add(identity)
                    if len(entered) == count:
                        all_entered.set()
                    await release.wait()
                return {'token': 'sandbox-token', 'sandbox': {'id': identity,
                    'state': 'terminated' if parameters['action'] == 'terminate' else 'ready',
                    'runtime_status': {'status': 'stopped' if parameters['action'] == 'terminate' else 'running'}}}
            raise AssertionError(operation)
        async def handle(request):
            message = decode(await request.read())
            return web.Response(body=encode({'result': await work(message['op'], message['params'])}))
        async with listener(handle) as port:
            endpoint = dict(hostname=socket.gethostname(), port=port, token='worker-token', workspace='/worker')
            if relay:
                endpoint['relay'] = 'a' * 32
            await asyncio.to_thread(seed, controller, endpoint, count, action)
            tasks = set()
            async def reply(request):
                result = await work(request['method'], request['parameters'])
                controller.relay.result(endpoint['relay'], request['id'], {'result': result})
            async def outbound():
                while not controller.relay.closed:
                    requests = await controller.relay.apoll(endpoint['relay'], limit=256)
                    for request in requests or ():
                        task = asyncio.create_task(reply(request))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
            pump = asyncio.create_task(outbound()) if relay else None
            started = time.perf_counter()
            futures = []
            try:
                for i in range(count):
                    identity = 'sw-' + str(i)
                    function = _claim if action == 'claim' else getattr(controller, '_' + ('launch' if action == 'create' else action))
                    args = (controller, 'pool-test', 'lease-' + str(i), identity) if action == 'claim' else (identity,)
                    futures.append(controller._submit(('allocation', identity), function, *args))
                await asyncio.wait_for(all_entered.wait(), 10)
                admitted = time.perf_counter() - started
                # No thread waits for a worker, even though every lifecycle is
                # still in flight. Unrelated transitions can run immediately.
                assert await asyncio.wait_for(asyncio.wrap_future(controller.executor.submit(lambda: True)), 1)
                unrelated = controller.state.get('allocation', 'sw-0')
                await asyncio.to_thread(controller.state.put, 'allocation', {**unrelated, 'id': 'unrelated',
                    'parent': None, 'lease': None, 'state': 'reserved', 'desired': 'running', 'generation': 1})
                before = time.perf_counter()
                launch = controller._submit(('allocation', 'unrelated'), controller._launch, 'unrelated')
                await asyncio.wait_for(asyncio.wrap_future(launch), 2)
                assert controller.allocation_get('unrelated')['state'] == 'ready'
                await asyncio.to_thread(controller.allocation_cancel, 'unrelated')
                cleanup = controller._submit(('allocation', 'unrelated'), controller._terminate, 'unrelated')
                await asyncio.wait_for(asyncio.wrap_future(cleanup), 2)
                assert controller.allocation_get('unrelated')['released']
                unrelated_seconds = time.perf_counter() - before
                for i, future in enumerate(futures):
                    assert not future.done()
                    assert controller._submit(('observe', 'sw-' + str(i)), controller._observe, 'sw-' + str(i)) is future
                sample = controller.state.get('allocation', 'sw-0')
                if action != 'terminate':
                    await asyncio.to_thread(controller.allocation_cancel, 'sw-0')
                    assert controller._submit(('allocation', 'sw-0'), controller._terminate, 'sw-0') is futures[0]
                    assert not any(k[0].startswith('sw-') and k[1] == 'terminate' for k in calls)
                release.set()
                await asyncio.wait_for(asyncio.gather(*(asyncio.wrap_future(f) for f in futures)), 10)
                assert all(v == 1 for v in calls.values())
                if action != 'terminate':
                    stale = controller.state.get('allocation', 'sw-0')
                    assert stale['generation'] == sample['generation'] + 1
                    assert stale['state'] != ('leased' if action == 'claim' else 'ready')
                    cleanup = controller._submit(('allocation', 'sw-0'), controller._terminate, 'sw-0')
                    await asyncio.wait_for(asyncio.wrap_future(cleanup), 2)
                    assert controller.allocation_get('sw-0')['released']
                for i in range(1, count):
                    allocation = controller.state.get('allocation', 'sw-' + str(i))
                    assert allocation['state'] == {'create': 'ready', 'claim': 'leased', 'terminate': 'terminated'}[action]
                print(json.dumps({'action': action, 'transport': 'relay' if relay else 'http',
                                  'simultaneous_waits': count, 'admission_seconds': admitted,
                                  'unrelated_create_and_terminate_seconds': unrelated_seconds}))
            finally:
                release.set()
                await asyncio.to_thread(controller.close)
                if pump:
                    pump.cancel()
                    await asyncio.gather(pump, *tasks, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize('action', ['create', 'claim', 'terminate'])
def test_shutdown_drains_waiting_lifecycle_before_state_and_executors_close(tmp_path, action):
    async def run():
        controller = Controller(tmp_path / 'controller')
        endpoint = dict(hostname='worker', port=1, workspace='/worker', token='token', relay='b' * 32)
        await asyncio.to_thread(seed, controller, endpoint, 256, action)
        futures = []
        for i in range(256):
            identity = 'sw-' + str(i)
            function = _claim if action == 'claim' else getattr(controller, '_' + ('launch' if action == 'create' else action))
            args = (controller, 'pool-test', 'lease-' + str(i), identity) if action == 'claim' else (identity,)
            futures.append(controller._submit(('allocation', identity), function, *args))
        deadline = time.monotonic() + 10
        while len(controller.relay.pending) != 256:
            assert time.monotonic() < deadline
            await asyncio.sleep(.01)
        await asyncio.wait_for(asyncio.to_thread(controller.close), 5)
        assert all(f.done() and f.exception() is None for f in futures)
        assert not controller.connections.thread.is_alive()
        assert not controller.relay.pending and not controller.relay.bytes
        restored = await asyncio.to_thread(Controller, tmp_path / 'controller')
        try:
            assert all(a['generation'] == (2 if action == 'claim' else 1) and not a['released']
                       and a['state'] == ('claiming' if action == 'claim' else 'unknown')
                       for a in restored.state.list('allocation'))
            assert all(l['state'] == ('claiming' if action == 'claim' else 'pending')
                       for l in restored.state.list('lease'))
        finally:
            await asyncio.to_thread(restored.close)
    asyncio.run(run())


def test_indexed_polling_bypasses_blocked_writer_and_control_threads(tmp_path, monkeypatch):
    async def run():
        controller = Controller(tmp_path / 'controller')
        endpoint = dict(hostname='worker', port=1, workspace='/worker', token='token')
        await asyncio.to_thread(seed, controller, endpoint, 256, 'claim')
        with controller.state.transaction():
            controller.state.put('lease', {'id': 'foreign', 'parent': 'another-pool', 'state': 'pending'})
            allocation = controller.state.get('allocation', 'sw-0')
            controller.state.put('allocation', {**allocation, 'state': 'leased'})
            lease = controller.state.get('lease', 'lease-0')
            controller.state.put('lease', {**lease, 'state': 'ready'})
            pool = controller.state.get('pool', 'pool-test')
            controller.state.put('pool', {**pool, 'request': {'expensive_payload': [dict(a=list(range(100)))] * 100}})
            for i in range(256):
                allocation = controller.state.get('allocation', 'sw-' + str(i))
                controller.state.put('allocation', {**allocation,
                    'spec': {'expensive_payload': [dict(a=list(range(100)))] * 100}})
        from sandweave.weave import state as state_module
        clone = state_module.clone
        def projected_only(value, **kwargs):
            assert not isinstance(value, dict) or 'expensive_payload' not in value, 'status copied an allocation definition'
            return clone(value, **kwargs)
        monkeypatch.setattr(state_module, 'clone', projected_only)
        dashboard = Dashboard(controller, 'controller-token')
        rpc = RPC(controller, 'controller-token', dashboard, asyncio.Event())
        release, disk_entered, writing = threading.Event(), threading.Event(), threading.Event()
        def stalled(db):
            disk_entered.set()
            assert release.wait(20)
        journal = controller.state._enqueue(operation=stalled)
        assert await asyncio.to_thread(disk_entered.wait, 2)
        def write():
            with controller.state.transaction():
                writing.set()
                controller.state.put('other', {'id': 'waiting-for-disk'})
        writer = asyncio.create_task(asyncio.to_thread(write))
        assert await asyncio.to_thread(writing.wait, 2)
        busy = [rpc.executor.submit(release.wait, 20) for _ in range(rpc.executor._max_workers)]
        async with listener(rpc.handle) as port:
            connection = AsyncConnection('127.0.0.1', port, 'controller-token', timeout=5)
            try:
                started = time.perf_counter()
                results = await asyncio.wait_for(asyncio.gather(*(
                    connection.call('pool_lease', identity='named-pool', lease_id='lease-' + str(i))
                    for i in range(256))), 3)
                assert results[0]['route']['endpoint']['token'] == 'sandbox-token'
                assert all(r['state'] == 'claiming' for r in results[1:])
                status = await asyncio.wait_for(connection.call('pool_status', identity='pool-test'), 1)
                assert status['active'] == 256 and len(status['sandboxes']) == 256
                assert status['retain_baseline'] is True
                with pytest.raises(PermissionError):
                    # Named lookup must still resolve before lease ownership is checked.
                    await connection.call('pool_lease', identity='pool-test', lease_id='foreign')
                print(json.dumps({'polls': len(results), 'poll_seconds': time.perf_counter() - started}))
            finally:
                release.set()
                await writer
                await asyncio.wrap_future(journal)
                await asyncio.gather(*(asyncio.wrap_future(f) for f in busy))
                await connection.close()
        await rpc.close()
        await asyncio.to_thread(dashboard.monitor.close)
        await asyncio.to_thread(controller.close)
    asyncio.run(run())


def test_failed_pool_poll_is_read_only_and_reports_unissued_failure(tmp_path):
    controller = Controller(tmp_path)
    try:
        seed(controller, {'hostname': 'worker', 'port': 1, 'token': 'token'}, 2, 'claim')
        pool = controller.state.get('pool', 'pool-test')
        controller.state.put('pool', {**pool, 'state': 'failed', 'error': 'image unavailable'})
        before = controller.state.get('lease', 'lease-1')
        result = dispatch(controller, 'pool_lease', {'identity': 'pool-test', 'lease_id': 'lease-1'})
        assert result['state'] == 'failed' and result['error'] == 'image unavailable'
        assert controller.state.get('lease', 'lease-1') == before
    finally:
        controller.close()

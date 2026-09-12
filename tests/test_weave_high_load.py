"""Work conservation and cancellation under concurrent image/worker failures."""
import asyncio
import json
import socket
import threading
import time

from aiohttp import web
import pytest

from sandweave import Memory
from sandweave.sandbox.connection import Connection
from sandweave.sandbox.errors import ResourceUnavailable
from sandweave.sandbox.sandbox import definition
from sandweave.sandbox.wire import encode
from sandweave.weave import artifacts, providers
from sandweave.weave.controller import Controller
from sandweave.weave.pool import _release_artifacts, dispatch
from test_weave import lab, Executor, make_pool, request, until


@pytest.mark.parametrize('relay', [False, True])
def test_explicit_loss_cancels_128_outstanding_requests(lab, monkeypatch, relay):
    controller = lab.controller
    monkeypatch.setattr(providers, 'direct', lambda endpoint, **kw:
                        Connection('127.0.0.1', endpoint['port'], endpoint['token'], **kw))

    async def run():
        count, entered, release = 0, asyncio.Event(), asyncio.Event()
        async def blocked(request):
            nonlocal count
            await request.read()
            count += 1
            if count == 128:
                entered.set()
            await release.wait()
            return web.Response(body=encode({'result': 'late'}))
        app = web.Application()
        app.router.add_post('/rpc', blocked)
        runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0, backlog=1024)
        await site.start()
        worker = controller.state.get('worker', lab.workers[0]['id'])
        endpoint = {**worker['endpoint'], 'port': site._server.sockets[0].getsockname()[1]}
        if relay:
            endpoint['relay'] = 'a' * 32
        worker['endpoint'] = endpoint
        worker['instances'][endpoint['workspace']]['endpoint'] = endpoint
        controller.state.put('worker', worker)
        futures = [asyncio.create_task(controller.connection(endpoint).acall('blocked')) for _ in range(128)]
        try:
            if relay:
                deadline = time.monotonic() + 5
                while len(controller.relay.pending) != 128:
                    assert time.monotonic() < deadline
                    await asyncio.sleep(.005)
            else:
                await asyncio.wait_for(entered.wait(), 5)
            started = time.perf_counter()
            await asyncio.to_thread(controller.worker_update, worker['id'], remove=True, lost=True)
            results = await asyncio.wait_for(asyncio.gather(
                *(asyncio.wrap_future(f) for f in futures), return_exceptions=True), 2)
            elapsed = time.perf_counter() - started
            assert all(isinstance(result, ResourceUnavailable) for result in results), results
            assert not controller.relay.pending and controller.relay.bytes == 0
            assert elapsed < 1
            with pytest.raises(ResourceUnavailable, match='declared lost'):
                controller.connection({**endpoint, 'port': endpoint['port'] + 1})
            # Neither another workspace on this host nor a replacement is lost.
            assert controller.connections.available({**endpoint, 'workspace': '/new-worker'})
            print(json.dumps({'transport': 'relay' if relay else 'http', 'cancelled': len(results),
                              'seconds': elapsed}))
        finally:
            release.set()
            await runner.cleanup()
    asyncio.run(run())


def test_lost_sources_and_cleanup_endpoints_are_skipped_after_restart(lab, monkeypatch):
    controller = lab.controller
    endpoints = [controller.state.get('worker', w['id'])['endpoint'] for w in lab.workers]
    reference = 'snap-' + 'b' * 32
    pool = make_pool(controller, warm=0)
    controller.state.put('artifact', dict(id=reference, info={'source': 'builder'},
        spec={'reference': reference}, locations=endpoints))
    controller.worker_update(lab.workers[0]['id'], remove=True, lost=True)
    assert controller.state.get('artifact', reference)['locations'] == [endpoints[1]]
    controller.close()
    controller = lab.controller = Controller(lab.directory)
    # Old backups/allocations can still contain a historical listener port.
    stale = {**endpoints[0], 'port': 9999}
    controller.state.put('artifact', dict(id=reference, info={'source': 'builder'},
        spec={'reference': reference}, locations=[stale, endpoints[1]]))
    calls = []
    class Healthy:
        def call(self, operation, **parameters):
            calls.append(operation)
            return {'id': reference}
        def close(self): pass
    def connect(endpoint):
        assert endpoint == endpoints[1], 'contacted an explicitly lost endpoint'
        return Healthy()
    monkeypatch.setattr(controller, 'connection', connect)
    assert artifacts.dispatch(controller, 'snapshot_info', {'reference': reference}) == {'id': reference}
    record = controller.state.get('pool', pool)
    controller.state.put('pool', {**record, 'baseline': reference, 'desired': 'closed'})
    _release_artifacts(controller, pool)
    assert calls == ['snapshot_info', 'artifact_release']
    assert controller.state.get('pool', pool)['artifacts_released']


def test_controller_shutdown_cannot_report_unperformed_artifact_cleanup(lab):
    controller = lab.controller
    reference = 'snap-' + 'c' * 32
    pool = make_pool(controller, warm=0)
    endpoint = controller.state.get('worker', lab.workers[0]['id'])['endpoint']
    controller.state.put('artifact', dict(id=reference, info={'source': 'builder'}, locations=[endpoint]))
    record = controller.state.get('pool', pool)
    controller.state.put('pool', {**record, 'baseline': reference, 'desired': 'closed'})
    controller.connections.stop()
    _release_artifacts(controller, pool)
    result = controller.state.get('pool', pool)
    assert not result.get('artifacts_released')
    assert 'stopping' in result['cleanup_error']


def test_controller_shutdown_preserves_desired_launch_for_restart(lab):
    controller = lab.controller
    identity = request(controller)
    record = controller.state.get('allocation', identity)
    controller.state.put('allocation', {**record, 'state': 'reserved', 'worker': lab.workers[0]['id']})
    controller.stopping.set()
    controller.connections.stop()
    controller._launch(identity)
    record = controller.state.get('allocation', identity)
    assert record['desired'] == 'running' and not record['released']
    assert record['state'] == 'unknown' and record['generation'] == 1


def test_128_image_waiters_leave_launch_and_cleanup_threads_available(lab, monkeypatch):
    controller = lab.controller
    entered, release = threading.Event(), threading.Event()
    transfers = []
    original = Executor.call
    def call(self, operation, **params):
        if operation == 'artifact_cache_identity':
            return 'one-actual-shared-cache'
        if operation == 'artifact_cached':
            return {'ready': True}
        return original(self, operation, **params)
    monkeypatch.setattr(Executor, 'call', call)
    def publish(*args):
        transfers.append(args[1])
        entered.set()
        assert release.wait(15)
        return args[1]
    monkeypatch.setattr(artifacts, 'ensure', publish)
    for worker in lab.workers:
        controller.worker_update(worker['id'], remove=True)
        controller.worker_add(str(lab.workers.index(worker) + 1), slots=128, memory='16GiB')
    pool = make_pool(controller, size=128, warm=128)
    record = controller.state.get('pool', pool)
    record['request']['spec']['resources'] = definition(memory=Memory('64MiB', '64MiB'))['spec']['resources']
    controller.state.put('pool', {**record, 'shared_cache': '/same-image'})
    try:
        until(lab, lambda: len(controller.preparation.waiters) == 128, timeout=10)
        assert entered.is_set() and len(transfers) == 1
        assert sum(t.name.startswith('weave-image') for t in threading.enumerate()) == 1
        started = time.perf_counter()
        unrelated = request(controller)
        until(lab, lambda: controller.allocation_get(unrelated)['state'] == 'ready')
        launch = time.perf_counter() - started
        started = time.perf_counter()
        controller.allocation_cancel(unrelated)
        until(lab, lambda: controller.allocation_get(unrelated)['released'])
        cleanup = time.perf_counter() - started
        assert not release.is_set() and launch < 1 and cleanup < 1
        print(json.dumps({'waiting_launches': 128, 'image_transfers': len(transfers),
                          'unrelated_launch_seconds': launch, 'cleanup_seconds': cleanup}))
        release.set()
        until(lab, lambda: controller.pool_status(pool)['ready'] == 128, timeout=10)
        assert len(transfers) == 1
    finally:
        release.set()
        dispatch(controller, 'pool_close', {'identity': pool})
        until(lab, lambda: controller.pool_status(pool)['state'] == 'closed', timeout=10)

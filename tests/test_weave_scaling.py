"""Concurrent serving must not depend on database progress or historical size."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time

from aiohttp import web
import pytest

from sandweave.sandbox.async_connection import AsyncConnection
from sandweave.sandbox.connection import Connection
from sandweave.weave.controller import Controller
from sandweave.weave.dashboard import Dashboard
from sandweave.weave.server import RPC
from sandweave.weave.state import State


def test_sync_connections_are_exclusive_and_reused_across_calling_threads(tmp_path):
    from test_weave import Executor
    from test_weave_transport import executor_server
    executor = Executor(tmp_path / 'worker', 1)
    executor.call = lambda op, **params: params['value']
    with executor_server(executor) as port:
        connection = Connection('127.0.0.1', port, 'private-worker-token')
        try:
            # Sequential callers on different threads should share a socket.
            for value in range(12):
                with ThreadPoolExecutor(1) as threads:
                    assert threads.submit(connection.call, 'echo', value=value).result() == value
                assert executor.open_connections == 1
            with ThreadPoolExecutor(32) as threads:
                values = [bytes([i])*4096 for i in range(64)]
                futures = [threads.submit(connection.call, 'echo', value=v) for v in values]
                assert [f.result() for f in futures] == values
            deadline = time.monotonic()+3
            while executor.open_connections > 8 and time.monotonic() < deadline:
                time.sleep(.01)
            assert executor.open_connections <= 8
        finally:
            connection.close()


def test_memory_reads_continue_during_disk_commit_and_never_see_rollback(tmp_path):
    state = State(tmp_path)
    original = state.put('allocation', {'id': 'one', 'state': 'ready', 'released': False, 'owner': 'first', 'data': b'bytes'})
    disk_entered, release_disk = threading.Event(), threading.Event()
    def blocked_disk(db):
        disk_entered.set()
        assert release_disk.wait(5)
    waiting = state._enqueue(operation=blocked_disk)
    try:
        assert disk_entered.wait(2)
        with state.transaction(deferred=True):
            state.put('worker', {'id': 'node', 'state': 'ready', 'telemetry': {'cpu': 4}})
        with ThreadPoolExecutor(2) as threads:
            write = threads.submit(state.put, 'allocation', {**original, 'owner': 'second'})
            def read():
                assert state.get('worker', 'node')['telemetry'] == {'cpu': 4}
                assert state.get('allocation', 'one')['owner'] == 'first'
                return state.list('allocation', owner='first', released=False)
            assert len(threads.submit(read).result(timeout=1)) == 1
            release_disk.set()
            write.result(timeout=2)
        assert not state.list('allocation', owner='first')
        assert state.list('allocation', owner='second')[0]['data'] == b'bytes'
        with pytest.raises(ValueError):
            with state.transaction():
                state.put('allocation', {**original, 'owner': 'uncommitted'})
                assert state.list('allocation', owner='uncommitted')
                raise ValueError('rollback')
        assert not state.list('allocation', owner='uncommitted')
        copy = state.get('worker', 'node')
        copy['telemetry']['cpu'] = -1
        assert state.get('worker', 'node')['telemetry']['cpu'] == 4
        waiting.result()
    finally:
        release_disk.set()
        state.close()
    restored = State(tmp_path)
    try:
        assert restored.list('allocation', owner='second')[0]['data'] == b'bytes'
        assert restored.get('worker', 'node')['telemetry']['cpu'] == 4
    finally:
        restored.close()


def test_backup_failure_does_not_poison_future_commits(tmp_path):
    state = State(tmp_path)
    try:
        def fail(db):
            raise OSError('backup destination unavailable')
        with pytest.raises(OSError):
            state._enqueue(operation=fail).result(timeout=2)
        assert state.put('item', {'id': 'still-writable'})['version'] == 1
    finally:
        state.close()


def test_monitor_reuses_history_and_refreshes_changed_terminal_records(tmp_path):
    from test_dashboard import seed
    from sandweave.weave.monitor import Monitor
    controller = Controller(tmp_path / 'state with ? and #')
    monitor = Monitor(controller)
    try:
        seed(controller, count=3)
        record = controller.state.get('allocation', 'sandbox-0')
        controller.state.put('allocation', {**record, 'state': 'terminated', 'released': True})
        monitor.refresh()
        original = monitor.detail('sandboxes', 'sandbox-0')
        assert original['telemetry'] == {}
        monitor.refresh()
        assert monitor.detail('sandboxes', 'sandbox-0') == original
        record = controller.state.get('allocation', 'sandbox-0')
        controller.state.put('allocation', {**record, 'error': 'updated history'}, event={'message': 'updated'})
        monitor.refresh()
        assert monitor.detail('sandboxes', 'sandbox-0')['error'] == 'updated history'
        assert original.get('error') is None
        assert controller.state.events()[-1]['detail'] == {'message': 'updated'}
    finally:
        monitor.close()
        controller.close()


def test_async_transport_closes_when_loop_ends_and_handle_can_use_a_new_loop():
    handle = Connection('localhost', 1, 'test')
    sessions = []
    async def use():
        from sandweave.sandbox.errors import OperationUnknown
        with pytest.raises(OperationUnknown):
            await handle.acall('ping')
        sessions.extend(c.session for c in handle.async_connections.values())
    for _ in range(2):
        asyncio.run(use())
        assert all(session.closed for session in sessions)
    handle.close()


def test_async_relay_cancellation_timeout_and_late_responses():
    from sandweave.weave.relay import Broker
    from sandweave.sandbox.errors import OperationUnknown
    async def run():
        broker = Broker()
        endpoint = {'relay': 'a'*32, 'token': 'test'}
        queued = asyncio.create_task(broker.acall(endpoint, 'mutate', {}, 10))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert not broker.pending and not broker.queues and broker.bytes == 0
        delivered = asyncio.create_task(broker.acall(endpoint, 'mutate', {}, .01))
        request = await broker.apoll(endpoint['relay'])
        with pytest.raises(OperationUnknown):
            await delivered
        broker.result(endpoint['relay'], request['id'], {'result': 'late'})
        assert not broker.pending and not broker.queues and broker.bytes == 0
        pending = asyncio.create_task(broker.acall(endpoint, 'mutate', {}, 10))
        request = await broker.apoll(endpoint['relay'])
        broker.result('b'*32, request['id'], {'result': 'wrong worker'})
        assert not pending.done()
        broker.result(endpoint['relay'], request['id'], {'result': 'correct'})
        assert await pending == 'correct'
        broker.close()
    asyncio.run(run())


def test_thousand_inflight_rpcs_bypass_persistence_and_preserve_correlation(tmp_path):
    async def run():
        controller = Controller(tmp_path / 'controller')
        channel = 'a' * 32
        with controller.state.transaction():
            for i in range(10000):
                controller.state.put('allocation', {'id': 'history-' + str(i), 'state': 'terminated',
                    'released': True, 'spec': {'setup': 'x' * 1000}})
            controller.state.put('allocation', {'id': 'live', 'state': 'ready', 'released': False,
                'token': 'scoped-token', 'endpoint': {'relay': channel, 'hostname': 'worker', 'port': 1}})
        dashboard = Dashboard(controller, 'controller-token')
        rpc = RPC(controller, 'controller-token', dashboard, asyncio.Event())
        app = web.Application()
        app.router.add_route('*', '/{path:.*}', rpc.handle)
        runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0, backlog=4096)
        await site.start()
        connection = AsyncConnection('127.0.0.1', site._server.sockets[0].getsockname()[1], 'controller-token', timeout=10)
        disk_entered, release_disk = threading.Event(), threading.Event()
        def stalled(db):
            disk_entered.set()
            assert release_disk.wait(20)
        journal = controller.state._enqueue(operation=stalled)
        assert await asyncio.to_thread(disk_entered.wait, 2)
        starting_threads = threading.active_count()
        async def worker():
            requests = []
            # No request may complete until all 1,000 are admitted simultaneously.
            while len(requests) < 1000:
                requests.extend(await controller.relay.apoll(channel, limit=64))
            assert threading.active_count() <= starting_threads + 2
            for request in reversed(requests):
                assert request['endpoint']['token'] == 'scoped-token'
                controller.relay.result(channel, request['id'], {'result': request['parameters']['payload']})
        remote = asyncio.create_task(worker())
        try:
            values = await asyncio.wait_for(asyncio.gather(*(connection.call('sandbox_rpc', identity='live',
                method='echo', parameters={'payload': i.to_bytes(4, 'big')}) for i in range(1000))), 15)
            assert values == [i.to_bytes(4, 'big') for i in range(1000)]
            await remote
            assert controller.relay.pending == {}
            assert controller.relay.bytes == 0
        finally:
            release_disk.set()
            await asyncio.to_thread(journal.result, 5)
            remote.cancel()
            await asyncio.gather(remote, return_exceptions=True)
            await connection.close()
            controller.relay.close()
            await runner.cleanup()
            await rpc.close()
            dashboard.monitor.close()
            controller.close()
    asyncio.run(run())

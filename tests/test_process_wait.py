"""Completion notifications, compatible polling and unlocked worker waits."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from sandweave.sandbox.guest_agent import Agent
from sandweave.sandbox.process import Process
from sandweave.sandbox.worker import Worker


@pytest.fixture
def agent(tmp_path):
    value = Agent(tmp_path / 'processes')
    yield value
    for identity in list(value.processes):
        value.terminate(identity)
        value.wait(identity, timeout=5)


def spawn(agent, code, identity='p', **options):
    return agent.spawn(identity, [sys.executable, '-u', '-c', code], cwd=str(agent.root),
                       user=str(os.getuid()), **options)


def test_guest_wait_wakes_after_complete_output_and_without_status_polling(agent):
    spawn(agent, 'import sys; sys.stdin.readline(); print("x" * 200000)')
    calls, status = [], agent.status
    def counted(identity):
        calls.append(identity)
        return status(identity)
    agent.status = counted
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(agent.wait, 'p', timeout=3)
        time.sleep(.1)
        assert not future.done() and calls == []
        agent.stdin('p', b'go\n')
        result = future.result(timeout=3)
    assert result['returncode'] == 0
    assert result['stdout_size'] == 200001
    assert calls == ['p']
    assert agent.output('p', 'stdout', size=300000) == b'x' * 200000 + b'\n'


def test_guest_stream_wait_and_exit_do_not_lose_notifications(agent):
    spawn(agent, 'import sys; sys.stdin.readline(); print("first"); sys.stdin.readline()')
    with ThreadPoolExecutor(2) as executor:
        output = executor.submit(agent.wait, 'p', stream='stdout', offset=0, timeout=3)
        done = executor.submit(agent.wait, 'p', timeout=3)
        agent.stdin('p', b'go\n')
        state = output.result(timeout=2)
        assert state['stdout_size'] == 6 and state['returncode'] is None
        assert not done.done()
        # Output already present must return immediately without a new event.
        assert agent.wait('p', stream='stdout', timeout=3)['stdout_size'] == 6
        agent.stdin('p', b'end\n')
        assert done.result(timeout=2)['returncode'] == 0
    assert agent.wait('p', stream='stderr', timeout=3)['returncode'] == 0


def test_guest_wait_timeout_does_not_kill_and_waiters_share_completion(agent):
    spawn(agent, 'import sys; sys.stdin.readline()')
    start = time.monotonic()
    assert agent.wait('p', timeout=.03)['returncode'] is None
    assert .02 < time.monotonic() - start < .5
    with ThreadPoolExecutor(8) as executor:
        pending = [executor.submit(agent.wait, 'p', timeout=3) for _ in range(8)]
        agent.stdin('p', close=True)
        assert [f.result(timeout=3)['returncode'] for f in pending] == [0] * 8


@pytest.mark.parametrize('options', [{'timeout': -1}, {'timeout': 11}, {'timeout': float('nan')},
                                    {'stream': 'bad'}, {'offset': -1}])
def test_invalid_wait(agent, options):
    with pytest.raises(ValueError, match='invalid process wait'):
        agent.wait('p', **options)


class GuestSandbox:
    def __init__(self, agent, *, legacy=False):
        self.agent, self.legacy, self.calls, self.id = agent, legacy, [], 'sandbox'

    def _call(self, operation, process_id, **params):
        self.calls.append((operation, params))
        if operation == 'process_status':
            result = self.agent.status(process_id)
            return result if self.legacy else {**result, 'wait_supported': True}
        return self.agent.call(operation.removeprefix('process_'), {'identity': process_id, **params})

    async def _acall(self, operation, **params):
        return await asyncio.to_thread(self._call, operation, **params)


@pytest.mark.parametrize('asynchronous', [False, True])
def test_sdk_wait_deadlines_and_notification_count(agent, asynchronous):
    spawn(agent, 'import sys; sys.stdin.readline(); print("done")')
    sandbox = GuestSandbox(agent)
    process = Process(sandbox, 'p')
    async def async_wait():
        with pytest.raises(TimeoutError, match='command is still running'):
            await process.wait.aio(timeout=.03)
        agent.stdin('p', close=True)
        assert await process.wait.aio(timeout=3) == 0
        assert (await process.result.aio()).stdout == 'done\n'
    if asynchronous:
        asyncio.run(async_wait())
    else:
        with pytest.raises(TimeoutError, match='command is still running'):
            process.wait(timeout=.03)
        agent.stdin('p', close=True)
        assert process.wait(timeout=3) == 0
        assert process.result().stdout == 'done\n'
    assert sum(op == 'process_status' for op, _ in sandbox.calls) == 1
    assert sum(op == 'process_wait' for op, _ in sandbox.calls) == 2


@pytest.mark.parametrize('asynchronous', [False, True])
def test_old_worker_backoff_is_bounded_and_respects_deadline(agent, asynchronous):
    spawn(agent, 'import sys; sys.stdin.readline()')
    sandbox = GuestSandbox(agent, legacy=True)
    process = Process(sandbox, 'p')
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        if asynchronous:
            asyncio.run(process.wait.aio(timeout=.6))
        else:
            process.wait(timeout=.6)
    assert .55 < time.monotonic() - start < 1
    assert 4 <= len(sandbox.calls) <= 10
    assert all(op == 'process_status' for op, _ in sandbox.calls)
    agent.stdin('p', close=True)
    assert process.wait(timeout=3) == 0


@pytest.mark.parametrize('asynchronous', [False, True])
def test_stream_notifications_preserve_partial_unicode_and_final_bytes(agent, asynchronous):
    spawn(agent, 'import os,time; time.sleep(.1); os.write(1, b"\\xe2"); '
                 'time.sleep(.1); os.write(1,b"\\x82\\xac\\nlast")')
    sandbox = GuestSandbox(agent)
    process = Process(sandbox, 'p')
    async def exercise():
        assert await process.stdout.readline.aio() == '€\n'
        assert await process.stdout.read.aio() == 'last'
    if asynchronous:
        asyncio.run(exercise())
    else:
        assert process.stdout.readline() == '€\n'
        assert process.stdout.read() == 'last'
    assert sum(op == 'process_status' for op, _ in sandbox.calls) == 1
    assert sum(op == 'process_wait' for op, _ in sandbox.calls) <= 4


def worker(tmp_path, agent):
    value = object.__new__(Worker)
    value.root, value.records = tmp_path, tmp_path / 'sandboxes'
    value.records.mkdir()
    value.guard, value.locks = threading.RLock(), {}
    class Connection:
        def call(self, operation, **params):
            return agent.call(operation, params)
    connection = Connection()
    value.runtime = SimpleNamespace(adapter=lambda name: SimpleNamespace(agent=lambda *args: connection))
    value.write({'id': 'sandbox', 'state': 'ready', 'agent': {}, 'spec': {'runtime': 'gvisor'}})
    return value, connection


def test_worker_reads_record_once_and_releases_lifecycle_lock(tmp_path, agent):
    value, _ = worker(tmp_path, agent)
    spawn(agent, 'import sys; sys.stdin.readline()')
    calls, read = [], value.read
    def counted(identity):
        calls.append(identity)
        return read(identity)
    value.read = counted
    assert value.dispatch('process_status', dict(identity='sandbox', process_id='p'))['wait_supported']
    assert calls == ['sandbox']
    entered, wait = threading.Event(), agent.wait
    def waiting(**params):
        entered.set()
        return wait(**params)
    agent.wait = waiting
    with ThreadPoolExecutor(1) as executor:
        pending = executor.submit(value.dispatch, 'process_wait', dict(identity='sandbox', process_id='p'))
        assert entered.wait(2)
        assert calls == ['sandbox', 'sandbox']
        # This dispatch takes the same lock: holding it across wait would deadlock.
        value.dispatch('process_stdin', dict(identity='sandbox', process_id='p', close=True))
        assert pending.result(timeout=2)['returncode'] == 0


def test_old_guest_fallback_reuses_agent_and_does_not_hide_errors(tmp_path, agent):
    value, connection = worker(tmp_path, agent)
    spawn(agent, 'import sys; sys.stdin.readline()')
    calls, call = [], connection.call
    def old(operation, **params):
        calls.append(operation)
        if operation == 'wait':
            raise ValueError('unknown agent operation')
        return call(operation, **params)
    connection.call = old
    assert value.process_wait('sandbox', 'p', timeout=.1)['returncode'] is None
    assert value.process_wait('sandbox', 'p', timeout=.1)['returncode'] is None
    assert calls.count('wait') == 1 and calls.count('status') <= 12
    def broken(*a, **kw):
        raise ValueError('actual guest failure')
    connection.call = broken
    connection._process_wait_supported = True
    with pytest.raises(ValueError, match='actual guest failure'):
        value.process_wait('sandbox', 'p')


def test_managed_wait_is_scoped_to_its_sandbox(tmp_path):
    from sandweave.sandbox.management import Management
    value = object.__new__(Management)
    value.read = lambda identity: {'id': 's1', 'token': 'secret'} if identity == 's1' else None
    assert value.authorize('sw1.s1.secret', 'process_wait', {'identity': 's1'})
    assert not value.authorize('sw1.s1.secret', 'process_wait', {'identity': 's2'})
    assert not value.authorize('sw1.s1.other', 'process_wait', {'identity': 's1'})


def test_service_wait_uses_async_transport_and_member_route():
    from sandweave.sandbox.services import ServiceConnection, Services
    calls = []
    async def acall(operation, **params):
        calls.append((operation, params))
        return {'returncode': 0}
    client = ServiceConnection(SimpleNamespace(acall=acall), 'group', 'db')
    assert asyncio.run(client.acall('process_wait', process_id='p')) == {'returncode': 0}
    assert calls == [('service_rpc', dict(identity='group', service='db', method='process_wait',
                                          parameters={'process_id': 'p'}))]
    service = object.__new__(Services)
    service.worker = SimpleNamespace(
        read=lambda identity: {'services': {'db': {'identity': 'member'}}},
        dispatch=lambda operation, params: (operation, params))
    assert service.dispatch('group', 'db', 'process_wait', {'process_id': 'p'}) == (
        'process_wait', {'identity': 'member', 'process_id': 'p'})

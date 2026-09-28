"""Durable deletion, retry, isolation, and controller generation handling."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
from types import SimpleNamespace

import pytest

from sandweave.sandbox.deletion import Deletions
from sandweave.sandbox.retention import remove
from sandweave.sandbox.sandbox import Sandbox
from test_worker_inventory import bare_worker
from test_weave import lab, request, until


def wait_for(test):
    deadline = time.monotonic() + 5
    while not test():
        assert time.monotonic() < deadline
        time.sleep(.01)


def prepared(tmp_path):
    worker = bare_worker(tmp_path)
    worker.controls = {}
    states = {}
    worker.runtime = SimpleNamespace(status=lambda identity: {'status': states.get(identity, 'stopped')},
        terminate=lambda identity: states.update({identity: 'stopped'}))
    adapter = SimpleNamespace(delete=lambda identity: remove(tmp_path/'private'/identity))
    worker.runtime.adapter = lambda name: adapter
    for identity in ('delete-me', 'keep-me'):
        states[identity] = 'running'
        worker.write(dict(id=identity, state='ready', spec={'runtime': 'gvisor'},
                          agent={'token': 'private'}, operation_id=identity))
        path = tmp_path/'private'/identity
        path.mkdir(parents=True)
        (path/'payload').write_text(identity)
    worker.deletions = Deletions(worker)
    return worker, adapter


def test_deletion_is_idempotent_and_preserves_snapshots_and_other_sandboxes(tmp_path):
    worker, adapter = prepared(tmp_path)
    saved = tmp_path/'published-snapshot'
    saved.write_text('keep')
    try:
        with ThreadPoolExecutor(8) as executor:
            results = list(executor.map(lambda _: worker.delete('delete-me'), range(16)))
        wait_for(lambda: worker.delete_status('delete-me')['state'] == 'deleted')
        assert all(r['state'] in ('pending', 'deleted') for r in results)
        assert not (tmp_path/'private/delete-me').exists()
        assert (tmp_path/'private/keep-me/payload').read_text() == 'keep-me'
        assert saved.read_text() == 'keep'
        assert worker.read('delete-me')['deleted']
        assert 'agent' not in worker.read('delete-me')
        assert worker.active == {'keep-me'}
        assert worker.delete('delete-me')['state'] == 'deleted'
    finally:
        worker.deletions.close()


def test_failed_deletion_resumes_after_restart_without_client(tmp_path):
    worker, adapter = prepared(tmp_path)
    original = adapter.delete
    def fail(identity):
        raise OSError('temporarily unavailable')
    adapter.delete = fail
    worker.delete('delete-me')
    wait_for(lambda: 'error' in worker.delete_status('delete-me'))
    worker.deletions.close()
    assert (tmp_path/'private/delete-me/payload').exists()
    adapter.delete = original
    worker.deletions = Deletions(worker)
    try:
        def retry():
            worker.deletions.tick()
            return worker.delete_status('delete-me')['state'] == 'deleted'
        wait_for(retry)
        assert not (tmp_path/'private/delete-me').exists()
    finally:
        worker.deletions.close()


def test_slow_delete_does_not_block_other_deletions_or_duplicate_requests(tmp_path):
    worker, adapter = prepared(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = adapter.delete
    def slow(identity):
        if identity == 'delete-me':
            entered.set()
            assert release.wait(5)
        original(identity)
    adapter.delete = slow
    try:
        worker.delete('delete-me')
        assert entered.wait(2)
        assert worker.delete('delete-me')['state'] == 'pending'
        worker.delete('keep-me')
        wait_for(lambda: worker.delete_status('keep-me')['state'] == 'deleted')
    finally:
        release.set()
        worker.deletions.close()


def test_sdk_nonblocking_delete_and_timeout_leave_cleanup_requested():
    env = Sandbox.__new__(Sandbox)
    env.id, env._terminated = 'sandbox', False
    calls = []
    def call(operation, **params):
        calls.append(operation)
        return {'id': 'sandbox', 'state': 'pending'}
    env._connection = SimpleNamespace(call=call)
    assert env.delete(wait=False)['state'] == 'pending'
    with pytest.raises(TimeoutError, match='cleanup will continue'):
        env.delete(timeout=.001)
    assert calls.count('delete') == 2


def test_async_delete_waits_without_synchronous_rpc_calls():
    import asyncio
    env = Sandbox.__new__(Sandbox)
    env.id, env._terminated = 'sandbox', False
    calls = []
    async def acall(operation, **params):
        calls.append(operation)
        return {'id': 'sandbox', 'state': 'pending' if operation == 'delete' else 'deleted'}
    env._connection = SimpleNamespace(acall=acall)
    assert asyncio.run(env.delete.aio())['state'] == 'deleted'
    assert calls == ['delete', 'delete_status']


def test_deletion_queue_is_bounded_and_inventory_remains_available(tmp_path):
    worker, adapter = prepared(tmp_path)
    release = threading.Event()
    def slow(identity):
        assert release.wait(5)
    adapter.delete = slow
    try:
        for number in range(32):
            identity = 'queued-' + str(number)
            worker.write(dict(id=identity, state='terminated', spec={'runtime': 'gvisor'}))
            worker.delete(identity)
        assert len(worker.deletions.running) <= 4
        assert len(worker.deletions.pending) == 32
        assert worker.read('keep-me')['state'] == 'ready'
    finally:
        release.set()
        worker.deletions.close()


def test_controller_deletion_does_not_repeat_blocking_termination(lab):
    controller = lab.controller
    identity = request(controller)
    until(lab, lambda: controller.allocation_get(identity)['state'] == 'ready')
    allocation = controller.state.get('allocation', identity)
    executor = lab.executors[allocation['endpoint']['port']]
    original = executor.call
    calls = []
    complete = False
    def call(operation, **params):
        calls.append(operation)
        if operation == 'ping':
            return {**original(operation, **params), 'sandbox_deletion': 1}
        if operation in ('delete', 'delete_status'):
            return {'state': 'deleted' if complete else 'pending'}
        return original(operation, **params)
    executor.call = call
    controller.allocation_delete(identity)
    generation = controller.state.get('allocation', identity)['generation']
    controller._terminate(identity)
    assert controller.state.get('allocation', identity)['deletion_started']
    assert controller.allocation_get(identity)['released']
    for _ in range(4):
        controller.allocation_delete(identity)
        controller._terminate(identity)
    assert calls.count('managed_apply') == 1

    assert controller.state.get('allocation', identity)['generation'] == generation
    from sandweave.weave.controller import Controller
    controller.close()
    controller = lab.controller = Controller(lab.directory, connector=controller.connector)
    assert controller.state.list('allocation', released=True, deletion_pending=True)[0]['id'] == identity
    complete = True
    until(lab, lambda: controller.allocation_get(identity).get('deleted'))
    assert controller.allocation_get(identity)['deleted']
    assert calls.count('managed_apply') == 1


def test_controller_deletes_reserved_request_before_worker_creation(lab):
    controller = lab.controller
    identity = request(controller)
    worker = controller.state.list('worker')[0]
    record = controller.state.get('allocation', identity)
    controller.state.put('allocation', {**record, 'worker': worker['id'], 'state': 'reserved'})
    executor = lab.executors[worker['endpoint']['port']]
    original = executor.call
    def call(operation, **params):
        if operation == 'ping':
            return {**original(operation, **params), 'sandbox_deletion': 1}
        assert operation != 'delete', 'there are no worker files to delete'
        return original(operation, **params)
    executor.call = call
    controller.allocation_delete(identity)
    controller._terminate(identity)
    assert controller.allocation_get(identity)['deleted']
    assert not executor.path(identity).exists()

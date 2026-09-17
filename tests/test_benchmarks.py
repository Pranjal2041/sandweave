from contextlib import contextmanager
import asyncio
import threading
import time

import pytest

from sandweave import Benchmark
from sandweave.benchmarks import Evaluation, TaskSpec
from sandweave.sandbox.pool import Pool as LocalPool
from sandweave.sandbox.pool import Lease
from sandweave.sandbox.asyncio import dualmethod
from sandweave.sandbox.sandbox import Sandbox


class FakeEnv(dict):
    close = Sandbox.__dict__['close']

    def __init__(self):
        self.connection_closes = 0

    def _close_connection(self):
        if not self.connection_closes:
            self.connection_closes += 1


class Suite:
    tasks = tuple(TaskSpec(str(i), 'instruction ' + str(i)) for i in range(12))

    def prepare(self):
        return {'template': 'coding'}

    def setup(self, env, task):
        assert env == {}
        env['task'] = task.id

    def evaluate(self, env, task):
        return Evaluation(task.id, float(env.get('solved', False)), bool(env.get('solved', False)))


class FakePool:
    instances = []

    def __init__(self, *, size, warm, **options):
        self.size, self.warm, self.options = size, warm, options
        self.lock = threading.Lock()
        self.active = self.peak = self.created = self.released = 0
        self.closed = False
        self.slots = threading.BoundedSemaphore(size)
        self.instances.append(self)

    @dualmethod
    def start(self):
        return self

    def acquire(self, *, timeout=None):
        cancelled = threading.Event()
        return Lease(self._acquire(cancelled, timeout), cancelled)

    @contextmanager
    def _acquire(self, cancelled, timeout):
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.slots.acquire(timeout=.01):
            if cancelled.is_set():
                raise InterruptedError('checkout cancelled')
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('capacity is in use')
        try:
            assert not self.closed
            with self.lock:
                self.created += 1
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                yield FakeEnv()
            finally:
                with self.lock:
                    self.active -= 1
                    self.released += 1
        finally:
            self.slots.release()

    map = LocalPool.__dict__['map']

    def close(self):
        self.closed = True


@pytest.fixture
def pools(monkeypatch):
    FakePool.instances = []
    monkeypatch.setattr('sandweave.weave.pool.Pool', FakePool)
    return FakePool.instances


def test_iteration_leases_clean_state_and_keeps_latest_evaluation(pools):
    bench = Benchmark(Suite(), capacity=3)
    with bench:
        for task in bench:
            with task as env:
                assert task.evaluate().score == 0
                env['solved'] = True
                assert task.evaluate().passed
            with pytest.raises(RuntimeError, match='active sandbox'):
                task.evaluate()
    assert len(bench.results) == 12
    assert all(result.passed for result in bench.results)
    assert pools[0].created == pools[0].released == 12
    assert pools[0].closed


def test_map_is_bounded_and_preserves_task_order(pools):
    barrier = threading.Barrier(3)

    def agent(env, instruction):
        assert instruction == 'instruction ' + env['task']
        barrier.wait(timeout=2)
        time.sleep((3 - int(env['task']) % 3) * .005)
        env['solved'] = True

    with Benchmark(Suite(), capacity=3, preload=1, cpu=2) as bench:
        results = list(bench.map(agent))
    pool = pools[0]
    assert pool.peak == 3
    assert pool.warm == 1
    assert pool.options == {'template': 'coding', 'cpu': 2}
    assert pool.created == pool.released == 12
    assert [result.task_id for result in results] == [str(i) for i in range(12)]
    assert all(result.passed for result in results)


@pytest.mark.parametrize('phase', ['setup', 'agent', 'evaluate'])
def test_errors_release_every_acquired_environment(pools, phase):
    class Broken(Suite):
        def setup(self, env, task):
            super().setup(env, task)
            if phase == 'setup' and task.id == '1':
                raise OSError('setup unavailable')

        def evaluate(self, env, task):
            if phase == 'evaluate' and task.id == '1':
                raise OSError('verifier unavailable')
            return super().evaluate(env, task)

    def agent(env, instruction):
        if phase == 'agent' and env['task'] == '1':
            raise OSError('agent unavailable')

    with Benchmark(Broken(), capacity=4) as bench:
        results = list(bench.map(agent, return_exceptions=True))
    assert isinstance(results[1], OSError)
    assert len(bench.results) == 11
    assert pools[0].active == 0
    assert pools[0].created == pools[0].released == 12


def test_setup_failure_in_manual_lease_is_released(pools):
    class Broken(Suite):
        def setup(self, env, task):
            raise OSError('missing asset')

    with Benchmark(Broken()) as bench:
        with pytest.raises(OSError, match='missing asset'):
            with bench.task('0'):
                pytest.fail('failed setup was exposed to the agent')
        assert not bench.results
        assert pools[0].released == 1


def test_attempt_cannot_be_reentered_and_closed_benchmark_cannot_restart(pools):
    with Benchmark(Suite()) as bench:
        task = bench.task('0')
        with task:
            with pytest.raises(RuntimeError, match='once'):
                task.__enter__()
        with pytest.raises(KeyError):
            bench.task('missing')
    with pytest.raises(RuntimeError, match='closed'):
        bench.start()


@pytest.mark.parametrize('options', [{'capacity': 0}, {'capacity': True}, {'preload': 2}, {'size': 2}])
def test_invalid_capacity_rejected_before_preparation(options):
    with pytest.raises(ValueError):
        Benchmark(Suite(), **options)


def test_duplicate_tasks_rejected():
    suite = Suite()
    suite.tasks = [TaskSpec('same', 'one'), TaskSpec('same', 'two')]
    with pytest.raises(ValueError, match='unique'):
        Benchmark(suite)


def test_evaluation_identity_must_match(pools):
    class Broken(Suite):
        def evaluate(self, env, task):
            return Evaluation('another task', 1, True)

    with Benchmark(Broken()) as bench:
        with bench.task('0') as env:
            with pytest.raises(ValueError, match='Evaluation'):
                bench._evaluate(env, bench.tasks[0])
        assert not bench.results


def test_async_agent_map_and_manual_context(pools):
    async def agent(env, instruction):
        await asyncio.sleep(.001)
        env['solved'] = True

    async def run():
        async with Benchmark(Suite(), capacity=3) as bench:
            results = [result async for result in bench.map.aio(agent)]
            assert all(result.passed for result in results)
            task = bench.task('0')
            async with task as env:
                env['solved'] = True
                assert (await task.evaluate.aio()).passed
        assert pools[0].created == pools[0].released == 13
    asyncio.run(run())


def test_cancel_during_setup_drains_before_release(pools):
    started, finish = threading.Event(), threading.Event()

    class Slow(Suite):
        def setup(self, env, task):
            started.set()
            assert finish.wait(5)
            assert pools[0].active == 1
            super().setup(env, task)

    async def run():
        async with Benchmark(Slow()) as bench:
            async def attempt():
                async with bench.task('0'):
                    pytest.fail('cancelled setup reached agent')
            pending = asyncio.create_task(attempt())
            await asyncio.to_thread(started.wait, 5)
            pending.cancel()
            await asyncio.sleep(.03)
            assert pools[0].active == 1
            pending.cancel()
            await asyncio.sleep(.03)
            assert pools[0].active == 1
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert pools[0].active == 0
    asyncio.run(run())


def test_128_tasks_at_64_concurrent_leases(pools):
    suite = Suite()
    suite.tasks = tuple(TaskSpec(str(i), str(i)) for i in range(128))
    barrier = threading.Barrier(64)
    def agent(env, instruction):
        barrier.wait(10)
        env['solved'] = True
    with Benchmark(suite, capacity=64) as bench:
        assert all(result.passed for result in bench.map(agent))
    assert pools[0].peak == 64
    assert pools[0].created == pools[0].released == 128


def test_evaluator_close_drains_inflight_initialization_and_calls(monkeypatch):
    from sandweave.benchmarks.evaluation import Evaluators
    import io
    started, finish = threading.Event(), threading.Event()
    evaluator = Evaluators('python', 'verifier')
    class Process:
        stdin = io.StringIO()
    process = Process()
    stopped = []
    def start():
        started.set()
        assert finish.wait(3)
        return process
    monkeypatch.setattr(evaluator, '_start', start)
    monkeypatch.setattr(evaluator, '_read', lambda *a: {'result': {'score': 100}})
    monkeypatch.setattr(evaluator, '_stop', stopped.append)
    result = []
    run = threading.Thread(target=lambda: result.append(evaluator.run({})))
    run.start()
    assert started.wait(3)
    close = threading.Thread(target=evaluator.close)
    close.start()
    close.join(.05)
    assert close.is_alive() and not stopped
    finish.set()
    run.join(3)
    close.join(3)
    assert not run.is_alive() and not close.is_alive()
    assert result == [{'score': 100}] and stopped == [process]


def test_pull_acquires_before_return_and_close_releases_capacity(pools):
    with Benchmark(Suite(), capacity=1) as bench:
        task = next(bench)
        env = task.env
        assert env['task'] == task.id == '0'
        assert pools[0].active == 1 and not bench.results
        env['solved'] = True
        assert task.evaluate().passed
        env.close()
        task.close()
        env.close()
        assert pools[0].active == 0 and pools[0].released == 1
        assert env.connection_closes == 1
        with pytest.raises(RuntimeError, match='active'):
            task.env
        second = bench.next()
        with second as env:
            assert env['task'] == '1'
        assert pools[0].released == 2


def test_pull_timeout_does_not_consume_task(pools):
    with Benchmark(Suite()) as bench:
        first = bench.next()
        with pytest.raises(TimeoutError):
            bench.next(timeout=.03)
        assert pools[0].active == 1
        first.close()
        with bench.next(timeout=1) as env:
            assert env['task'] == '1'
        assert pools[0].created == pools[0].released == 2


def test_concurrent_pullers_share_cursor_and_capacity(pools):
    from concurrent.futures import ThreadPoolExecutor
    suite = Suite()
    suite.tasks = tuple(TaskSpec(str(i), str(i)) for i in range(64))
    barrier = threading.Barrier(8)
    with Benchmark(suite, capacity=8) as bench:
        def client(_):
            task = bench.next(timeout=10)
            env = task.env
            try:
                barrier.wait(5)
                assert env['task'] == task.id
                env['solved'] = True
                return task.evaluate()
            finally:
                if int(task.id) % 2:
                    env.close()
                task.close()
        with ThreadPoolExecutor(16) as executor:
            results = list(executor.map(client, range(64)))
        assert {r.task_id for r in results} == {str(i) for i in range(64)}
        assert all(r.passed for r in results)
        assert pools[0].peak == 8
        assert pools[0].created == pools[0].released == 64
        with pytest.raises(StopIteration):
            next(bench)


def test_concurrent_task_and_env_close_release_once(pools):
    from concurrent.futures import ThreadPoolExecutor
    with Benchmark(Suite()) as bench:
        task = bench.next()
        env = task.env
        barrier = threading.Barrier(16)
        def close(index):
            barrier.wait(3)
            (task.close if index % 2 else env.close)()
        with ThreadPoolExecutor(16) as executor:
            list(executor.map(close, range(16)))
        assert pools[0].released == 1 and pools[0].active == 0
        assert env.connection_closes == 1


def test_benchmark_close_cancels_waiter_and_releases_manual_tasks(pools):
    from concurrent.futures import ThreadPoolExecutor
    bench = Benchmark(Suite())
    first = bench.next()
    with ThreadPoolExecutor(1) as executor:
        pending = executor.submit(bench.next)
        deadline = time.monotonic() + 3
        while len(bench._attempts) < 2 and time.monotonic() < deadline:
            time.sleep(.005)
        assert len(bench._attempts) == 2
        bench.close()
        with pytest.raises((InterruptedError, RuntimeError)):
            pending.result(3)
    first.close()
    assert not bench._attempts
    assert pools[0].active == 0 and pools[0].created == pools[0].released


def test_close_waits_for_inflight_evaluation(pools):
    from concurrent.futures import ThreadPoolExecutor
    started, finish = threading.Event(), threading.Event()
    class Slow(Suite):
        def evaluate(self, env, task):
            started.set()
            assert finish.wait(3)
            assert pools[0].active == 1
            return super().evaluate(env, task)
    with Benchmark(Slow()) as bench, ThreadPoolExecutor(2) as executor:
        task = bench.next()
        env = task.env
        pending = executor.submit(task.evaluate)
        assert started.wait(3)
        closed = executor.submit(env.close)
        time.sleep(.02)
        assert not closed.done() and pools[0].active == 1
        finish.set()
        pending.result(3)
        closed.result(3)
        assert pools[0].active == 0


def test_pull_setup_failure_releases_and_identified_retry_remains_possible(pools):
    class Broken(Suite):
        def setup(self, env, task):
            if task.id == '0':
                raise OSError('setup unavailable')
            super().setup(env, task)
    with Benchmark(Broken()) as bench:
        with pytest.raises(OSError, match='setup unavailable'):
            bench.next()
        assert pools[0].active == 0
        with bench.next() as env:
            assert env['task'] == '1'
        with pytest.raises(OSError):
            with bench.task('0'):
                pass


def test_async_pull_cancellation_does_not_leak_or_consume_waiting_task(pools):
    async def run():
        async with Benchmark(Suite()) as bench:
            first = await bench.next.aio()
            pending = asyncio.create_task(bench.next.aio())
            await asyncio.sleep(.03)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 1)
            assert pools[0].active == 1
            await first.env.close.aio()
            task = await bench.next.aio(timeout=1)
            assert task.id == '1'
            await task.close.aio()
            assert pools[0].created == pools[0].released == 2
    asyncio.run(run())


def test_async_pull_cancel_during_setup_drains_and_returns_task(pools):
    started, finish = threading.Event(), threading.Event()
    class Slow(Suite):
        def setup(self, env, task):
            started.set()
            assert finish.wait(3)
            assert pools[0].active == 1
            super().setup(env, task)
    async def run():
        async with Benchmark(Slow()) as bench:
            pending = asyncio.create_task(bench.next.aio())
            assert await asyncio.to_thread(started.wait, 3)
            pending.cancel()
            await asyncio.sleep(.02)
            pending.cancel()
            assert pools[0].active == 1
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert pools[0].active == 0
            task = await bench.next.aio()
            assert task.id == '0'
            await task.close.aio()
    asyncio.run(run())


def test_async_pull_exhaustion(pools):
    suite = Suite()
    suite.tasks = suite.tasks[:1]
    async def run():
        async with Benchmark(suite) as bench:
            task = await bench.next.aio()
            async with task as env:
                assert env['task'] == '0'
            with pytest.raises(StopAsyncIteration):
                await bench.next.aio()
    asyncio.run(run())


def test_async_waiters_cannot_starve_close_or_unrelated_calls(pools):
    suite = Suite()
    suite.tasks = tuple(TaskSpec(str(i), str(i)) for i in range(80))
    async def run():
        async with Benchmark(suite) as bench:
            first = await bench.next.aio()
            pending = [asyncio.create_task(bench.next.aio()) for _ in range(64)]
            await asyncio.sleep(.03)
            # More waiters than asyncio's default thread count; these operations
            # must still execute while the benchmark is at capacity.
            assert await asyncio.wait_for(asyncio.to_thread(lambda: 'independent'), 1) == 'independent'
            await asyncio.wait_for(first.env.close.aio(), 1)
            second = await asyncio.wait_for(pending[0], 1)
            assert second.id == '1'
            for future in pending[1:]:
                future.cancel()
            await asyncio.wait_for(asyncio.gather(*pending[1:], return_exceptions=True), 2)
            assert pools[0].active == 1
            await second.close.aio()
            third = await bench.next.aio()
            assert third.id == '2'
            await third.close.aio()
    asyncio.run(run())


def test_async_timeout_includes_acquisition_queue_wait(pools):
    async def run():
        async with Benchmark(Suite()) as bench:
            first = await bench.next.aio()
            waiting = asyncio.create_task(bench.next.aio())
            await asyncio.sleep(.02)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(bench.next.aio(timeout=.03), .5)
            assert pools[0].active == 1
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            await first.close.aio()
            second = await bench.next.aio()
            assert second.id == '1'
            await second.close.aio()
    asyncio.run(run())

"""Pull real task leases through the local pool and an HTTP Weave controller."""
from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
import os
import threading
import time

import pytest

from sandweave import Benchmark, Memory, Sandbox
from sandweave.benchmarks import Evaluation, TaskSpec
from sandweave.weave.transport import join_link
from test_weave_live import cluster, wait_for

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_WEAVE_INTEGRATION'), reason='explicit disposable workers required')]


class Arithmetic:
    tasks = tuple(TaskSpec(str(i), str(i)) for i in range(25))

    def prepare(self):
        return {'template': 'coding', 'memory': Memory('256MiB', '256MiB')}

    def setup(self, env, task):
        assert env.run('test ! -e /workspace/answer', check=True).returncode == 0

    def evaluate(self, env, task):
        passed = env.files.read_text('/workspace/answer').strip() == str(int(task.id) * 2)
        return Evaluation(task.id, 100.0 if passed else 0.0, passed)


def exercise(target=None):
    identities, active, peak = set(), set(), [0]
    lock = threading.Lock()
    started = time.monotonic()
    with Sandbox(memory=Memory('256MiB', '256MiB')) as unrelated:
        with Benchmark(Arithmetic(), capacity=8, target=target) as bench:
            def solve(task):
                env = task.env
                with lock:
                    assert env.id not in identities
                    identities.add(env.id)
                env.run('printf ' + str(int(task.id) * 2) + ' > /workspace/answer', check=True)
                assert task.evaluate().passed

            initial = [next(bench) for _ in range(8)]
            with pytest.raises(TimeoutError):
                bench.next(timeout=.1)
            assert unrelated.run('printf independent', timeout=10).stdout == 'independent'
            solve(initial[0])
            initial[0].env.close()
            initial[0].close()
            replacement = bench.next(timeout=120)
            assert replacement.id == '8', 'timed-out checkout consumed a task'
            for task in initial[1:] + [replacement]:
                solve(task)
                task.close()
            barrier = threading.Barrier(8)
            def client(_):
                while True:
                    try:
                        task = bench.next(timeout=120)
                    except StopIteration:
                        return
                    with task as env:
                        with lock:
                            active.add(task.id)
                            peak[0] = max(peak[0], len(active))
                            assert len(active) <= 8
                        barrier.wait(120)
                        try:
                            solve(task)
                        finally:
                            with lock:
                                active.remove(task.id)
                        # Exercise env.close plus the subsequent task context exit.
                        if int(task.id) % 2:
                            env.close()
            with ThreadPoolExecutor(8) as executor:
                futures = [executor.submit(client, i) for i in range(8)]
                for _ in range(20):
                    assert unrelated.run('printf responsive', timeout=10).stdout == 'responsive'
                for future in futures:
                    future.result(180)
            assert len(bench.results) == len(identities) == 25
            assert peak[0] == 8 and not active
            pool = bench._pool
        assert not bench._attempts
        assert unrelated.run('printf still-alive').stdout == 'still-alive'
    print(json.dumps({'transport': 'http' if target else 'local', 'tasks': 25,
                      'peak_task_leases': peak[0], 'seconds': time.monotonic() - started}))
    return pool, identities


def test_local_pull_capacity_cleanup_and_unrelated_progress():
    pool, _ = exercise()
    assert not pool.all and not pool.active and not pool.pending


def test_http_pull_capacity_cleanup_and_unrelated_progress(cluster):
    assert sum(w['capacity']['slots'] for w in cluster.workers) >= 8
    url = join_link(cluster.info['connection']['address'], cluster.connection.token)
    pool, identities = exercise(url)
    wait_for(lambda: all(a['released'] for a in cluster.info['sandboxes'] if a['id'] in identities))
    records = [a for a in cluster.info['sandboxes'] if a['id'] in identities]
    assert len(records) == 25 and len({a['worker'] for a in records}) == 2
    assert pool.info['state'] == 'closed'


async def exercise_async(target=None):
    class ManyTasks(Arithmetic):
        tasks = tuple(TaskSpec(str(i), str(i)) for i in range(80))

    async with Benchmark(ManyTasks(), capacity=2, target=target) as bench:
        first, second = await bench.next.aio(), await bench.next.aio()
        pending = [asyncio.create_task(bench.next.aio()) for _ in range(64)]
        try:
            await asyncio.sleep(.2)
            assert not any(task.done() for task in pending)
            with pytest.raises(TimeoutError):
                await bench.next.aio(timeout=.1)
            # Blocked acquisitions must leave the shared executor usable.
            assert await asyncio.wait_for(asyncio.to_thread(lambda: 42), 5) == 42
            await asyncio.wait_for(first.env.close.aio(), 30)
            third = await asyncio.wait_for(asyncio.shield(pending[0]), 60)
            assert third.id == '2'
            for waiter in pending[1:]:
                waiter.cancel()
            results = await asyncio.wait_for(asyncio.gather(*pending[1:], return_exceptions=True), 30)
            assert all(isinstance(result, asyncio.CancelledError) for result in results)
            await third.env.run.aio('printf 4 > /workspace/answer', check=True)
            assert (await third.evaluate.aio()).passed
            await third.close.aio()
            replacement = await bench.next.aio(timeout=60)
            assert replacement.id == '3', 'cancelled checkout consumed a task'
            await replacement.env.close.aio()
            await second.close.aio()
        finally:
            for waiter in pending:
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        pool = bench._pool
    assert not bench._attempts
    return pool


def test_local_async_pull_cancellation_and_executor_progress():
    pool = asyncio.run(exercise_async())
    assert not pool.all and not pool.active and not pool.pending


def test_http_async_pull_cancellation_and_executor_progress(cluster):
    url = join_link(cluster.info['connection']['address'], cluster.connection.token)
    pool = asyncio.run(exercise_async(url))
    assert pool.info['state'] == 'closed'

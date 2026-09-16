"""Unmodified Harbor Trial + verifier run against disposable Sandweave guests."""
import asyncio
import os
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip('harbor')
from test_harbor import make_task
from sandweave import Benchmark, Memory, Sandbox
from test_weave_live import cluster
from sandweave.weave.transport import join_link

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_HARBOR_INTEGRATION'), reason='explicit disposable worker required')]


def test_positive_negative_rewards_hidden_tests_and_cleanup(tmp_path):
    make_task(tmp_path, 'a')
    make_task(tmp_path, 'b')
    bench = Benchmark('harbor', source=tmp_path, capacity=2, memory=Memory('256MiB', '256MiB'))
    try:
        task = bench.next()
        assert task.env.run('test ! -e /tests/test.sh').returncode == 0
        task.env.run('printf 1 > /answer', check=True)
        assert task.evaluate().rewards == {'correct': 1, 'cost': .25}
        assert task.evaluate().score is None
        task.close()
        task = bench.next()
        assert task.env.run('test ! -e /answer').returncode == 0
        assert task.evaluate().rewards == {'correct': 0, 'cost': .25}
        task.env.close()
    finally:
        bench.close()
    assert not bench._pool.live and not bench._attempts


def test_multi_step_keeps_guest_and_cleans_verifier_inputs(tmp_path):
    make_task(tmp_path, steps=True)
    bench = Benchmark('harbor', source=tmp_path, memory=Memory('256MiB', '256MiB'))
    try:
        task = bench.next()
        identity = task.env.id
        assert task.instruction == 'Write 1 into /answer'
        task.env.run('printf 1 > /answer', check=True)
        assert task.evaluate().rewards['correct'] == 1
        assert task.next_step() is task
        assert task.instruction == 'Write 2 into /answer'
        assert task.env.id == identity
        assert task.env.run('cat /answer').stdout == '1'
        assert task.env.run('test ! -e /tests/test.sh').returncode == 0
        task.env.run('printf 2 > /answer', check=True)
        assert task.evaluate().rewards['correct'] == 1
        with pytest.raises(StopIteration):
            task.next_step()
        task.close()
    finally:
        bench.close()


def exercise_concurrent(tmp_path, target=None):
    for index in range(12):
        make_task(tmp_path, f'task-{index:02}', image='ubuntu:22.04' if index % 2 else 'debian:bookworm-slim')
    bench = Benchmark('harbor', source=tmp_path, capacity=4, target=target,
                      memory=Memory('256MiB', '256MiB'))
    try:
        initial = [bench.next() for _ in range(4)]
        with pytest.raises(TimeoutError):
            bench.next(timeout=.05)
        with Sandbox(memory=Memory('256MiB', '256MiB')) as unrelated:
            assert unrelated.run('printf responsive').stdout == 'responsive'
        for task in initial:
            task.close()
        barrier = threading.Barrier(4)
        identities = set()
        lock = threading.Lock()
        def solve(_):
            task = bench.next(timeout=120)
            try:
                with lock:
                    assert task.env.id not in identities
                    identities.add(task.env.id)
                assert task.env.run('test ! -e /answer').returncode == 0
                task.env.run('printf 1 > /answer', check=True)
                barrier.wait(timeout=120)
                assert task.evaluate().rewards['correct'] == 1
            finally:
                task.close()
        with ThreadPoolExecutor(4) as executor:
            list(executor.map(solve, range(8)))
        assert len(identities) == 8
        assert len(bench._pool.prepared) == 2
    finally:
        bench.close()
    assert not bench._attempts and not bench._pool.live


def test_concurrent_mixed_images_share_one_capacity(tmp_path):
    exercise_concurrent(tmp_path)


@pytest.mark.skipif(not os.environ.get('SANDWEAVE_WEAVE_INTEGRATION'), reason='explicit cluster required')
def test_http_concurrent_mixed_images_and_cleanup(tmp_path, cluster):
    url = join_link(cluster.info['connection']['address'], cluster.connection.token)
    exercise_concurrent(tmp_path, url)
    assert all(pool['state'] == 'closed' for pool in cluster.info['pools'])


def test_async_waiters_cancellation(tmp_path):
    for index in range(8):
        make_task(tmp_path, str(index))
    async def run():
        bench = Benchmark('harbor', source=tmp_path, capacity=1, memory=Memory('256MiB', '256MiB'))
        try:
            task = await bench.next.aio()
            waiters = [asyncio.create_task(bench.next.aio()) for _ in range(64)]
            await asyncio.sleep(.1)
            with pytest.raises(TimeoutError):
                await bench.next.aio(timeout=.05)
            assert await asyncio.wait_for(asyncio.to_thread(lambda: True), 5)
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
            await task.env.run.aio('printf 1 > /answer', check=True)
            assert (await task.evaluate.aio()).rewards['correct'] == 1
            await task.close.aio()
            task = await bench.next.aio()
            assert task.id == '1'
            await task.env.close.aio()
        finally:
            await bench.close.aio()
    asyncio.run(run())


def test_transfers_keep_modes_links_and_complete_output(tmp_path):
    task_path = make_task(tmp_path / 'data')
    inputs = task_path / 'environment'
    (inputs / 'empty').mkdir()
    (inputs / 'executable').write_text('#!/bin/bash\nprintf inherited')
    (inputs / 'executable').chmod(0o755)
    (inputs / 'link').symlink_to('executable')
    bench = Benchmark('harbor', source=task_path, memory=Memory('256MiB', '256MiB'))
    try:
        task = bench.next()
        assert task.env.run('test -d /empty && test -L /link && /link', check=True).stdout == 'inherited'
        provider = bench._pool.sessions[task.env.id].trial.agent_environment
        result = bench._suite.loop.call(provider.exec("head -c 2097152 /dev/zero | tr '\\0' x"))
        assert result.return_code == 0 and len(result.stdout) == 2097152
        task.env.run('mkdir -p /outputs/empty && cp -a /executable /link /outputs/', check=True)
        bench._suite.loop.call(provider.download_dir('/outputs', tmp_path / 'download'))
        assert (tmp_path / 'download/link').is_symlink()
        assert (tmp_path / 'download/executable').stat().st_mode & 0o111
        assert (tmp_path / 'download/empty').is_dir()
        task.close()
    finally:
        bench.close()


def test_separate_verifier_uses_another_clean_sandbox(tmp_path):
    path = make_task(tmp_path)
    config = path / 'task.toml'
    config.write_text(config.read_text() + '''
[verifier.environment]
docker_image = "ubuntu:22.04"
workdir = "/tests"
memory_mb = 256
''')
    (path / 'tests/test.sh').write_text('''#!/bin/bash
mkdir -p /logs/verifier
test ! -e /agent-private-file || exit 1
echo 1 > /logs/verifier/reward.txt
''')
    bench = Benchmark('harbor', source=path, memory=Memory('256MiB', '256MiB'))
    try:
        task = bench.next()
        task.env.run('touch /agent-private-file', check=True)
        result = task.evaluate()
        assert result.rewards == {'reward': 1.0}
        assert len(bench._pool.prepared) == 2
        task.close()
    finally:
        bench.close()


@pytest.mark.parametrize('asynchronous', [False, True])
def test_mapping_runs_all_steps(asynchronous, tmp_path):
    make_task(tmp_path, steps=True)
    bench = Benchmark('harbor', source=tmp_path, memory=Memory('256MiB', '256MiB'))
    def agent(env, instruction):
        env.run('printf ' + instruction.split()[1] + ' > /answer', check=True)
    async def run():
        return [result async for result in bench.map.aio(agent)]
    try:
        results = asyncio.run(run()) if asynchronous else list(bench.map(agent))
        assert len(results) == 1 and results[0].rewards['correct'] == 1
    finally:
        bench.close()


@pytest.mark.skipif(not os.environ.get('SANDWEAVE_HARBOR_TASK'), reason='explicit unchanged upstream task required')
def test_original_task_solution_and_verifier():
    source = Path(os.environ.get('SANDWEAVE_HARBOR_TASK', '.'))
    bench = Benchmark('harbor', source=source, capacity=1)
    try:
        task = bench.next()
        assert task.env.run('test ! -e /tests/test.sh').returncode == 0
        provider = bench._pool.sessions[task.env.id].trial.agent_environment
        bench._suite.loop.call(provider.upload_dir(source / 'solution', '/solution'))
        task.env.run('bash /solution/solve.sh', timeout=300, check=True)
        result = task.evaluate()
        assert result.rewards == {'reward': 1.0}, result
        print('Original solution:', result)
        task.close()
        retry = bench.task(task.id)
        with retry:
            result = retry.evaluate()
            assert result.rewards == {'reward': 0.0}, result
            print('Unsolved fresh task:', result)
    finally:
        bench.close()

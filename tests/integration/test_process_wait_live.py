"""Real waiting commands must not starve worker ownership or unrelated tools."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
import threading
import time
import uuid

import pytest

from sandweave import Memory, Sandbox
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit worker required')]


@pytest.fixture(scope='module', autouse=True)
def release_idle_test_worker():
    yield
    connection = local_connection()
    connection.call('_shutdown_if_idle')
    connection.close()


def summary(values):
    values = sorted(values)
    return {name: values[min(len(values)-1, int(len(values)*q))]
            for name, q in [('p50', .5), ('p95', .95), ('max', 1)]}


def cpu_seconds(pid):
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return sum(int(fields[i]) for i in (11, 12)) / os.sysconf('SC_CLK_TCK')


@pytest.mark.parametrize('asynchronous', [False, True])
def test_many_waiters_keep_worker_responsive(asynchronous, tmp_path):
    count = int(os.environ.get('SANDWEAVE_WAIT_CONCURRENCY', '128'))
    legacy = bool(os.environ.get('SANDWEAVE_WAIT_LEGACY'))
    calls, guard = {}, threading.Lock()
    samples = {'owner_register': [], 'list': [], 'run': []}
    connection = local_connection(template=__import__('sandweave').Template('coding').resolve())
    try:
        pid = connection.call('ping')['pid']
        with ExitStack() as stack:
            envs = [stack.enter_context(Sandbox(memory=Memory('512MiB', '512MiB'))) for _ in range(4)]
            probe = stack.enter_context(Sandbox(memory=Memory('256MiB', '256MiB')))
            processes = [envs[i % len(envs)].exec(argv=['python', '-c',
                'import sys; sys.stdin.read(); print("finished")']) for i in range(count)]
            for env in envs:
                original = env._call
                def counted(op, _call=original, **params):
                    with guard:
                        calls[op] = calls.get(op, 0) + 1
                    return _call(op, **params)
                original_async = env._acall
                async def acounted(op, _call=original_async, **params):
                    with guard:
                        calls[op] = calls.get(op, 0) + 1
                    return await _call(op, **params)
                env._call, env._acall = counted, acounted

            def probes():
                # Measure the steady state, after all callers enter their wait.
                time.sleep(1)
                started, cpu = time.monotonic(), cpu_seconds(pid)
                for _ in range(12):
                    for name, function in (
                        ('owner_register', lambda: connection.call('owner_register', identity=uuid.uuid4().hex, process=None)),
                        ('list', lambda: connection.call('list')),
                        ('run', lambda: probe.run('printf responsive', check=True))):
                        begin = time.monotonic()
                        result = function()
                        if name == 'run':
                            assert result.stdout == 'responsive'
                        samples[name].append(time.monotonic() - begin)
                    time.sleep(.2)
                elapsed = time.monotonic() - started
                return {'worker_cpu_fraction': (cpu_seconds(pid) - cpu) / elapsed,
                        'measured_seconds': elapsed}

            # The optional baseline reproduces the released synchronous polling
            # interval on the SAME worker, isolating the cost of client polling.
            def wait(process):
                if legacy:
                    while process.poll() is None:
                        time.sleep(.01)
                    return process.poll()
                return process.wait(timeout=60)

            async def await_process(process):
                if legacy:
                    while await process.poll.aio() is None:
                        await asyncio.sleep(.05)
                    return await process.poll.aio()
                return await process.wait.aio(timeout=60)

            async def async_run():
                pending = [asyncio.create_task(await_process(p)) for p in processes]
                try:
                    report = await asyncio.to_thread(probes)
                finally:
                    for p in processes:
                        await p.stdin.close.aio()
                assert await asyncio.gather(*pending) == [0] * count
                return report

            if asynchronous:
                report = asyncio.run(async_run())
            else:
                with ThreadPoolExecutor(count) as executor:
                    pending = [executor.submit(wait, p) for p in processes]
                    try:
                        report = probes()
                    finally:
                        for p in processes:
                            p.stdin.close()
                    assert [f.result(timeout=30) for f in pending] == [0] * count
            assert all(p.result().stdout == 'finished\n' for p in processes)
            report.update(waiters=count, asynchronous=asynchronous, legacy_polling=legacy,
                          calls=calls, latency_seconds={k: summary(v) for k, v in samples.items()})
            destination = Path(os.environ.get('SANDWEAVE_WAIT_REPORT', str(tmp_path)))
            destination.mkdir(parents=True, exist_ok=True)
            (destination / ('async.json' if asynchronous else 'sync.json')).write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps(report), flush=True)
            if not legacy:
                assert calls['process_status'] == count
                assert count <= calls['process_wait'] <= 2 * count
                assert max(samples['owner_register']) < 1
                assert max(samples['list']) < 2
                assert max(samples['run']) < 2
    finally:
        connection.close()


def test_async_wait_cancellation_and_termination_leave_worker_usable():
    async def run():
        async with await Sandbox.create.aio() as env:
            process = await env.exec.aio('sleep 60')
            pending = asyncio.create_task(process.wait.aio())
            await asyncio.sleep(.1)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert await process.poll.aio() is None
            assert (await env.run.aio('printf alive')).stdout == 'alive'
            assert await process.terminate.aio() != 0
            assert (await env.run.aio('printf alive')).stdout == 'alive'
    asyncio.run(run())

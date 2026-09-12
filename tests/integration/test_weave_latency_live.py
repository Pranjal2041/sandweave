"""Measure the public async SDK through two disposable outbound-relay workers."""
import asyncio
import json
import os
from pathlib import Path
import time

from sandweave import Memory, Pool
from test_weave_live import cluster, pytestmark


def summary(values):
    values = sorted(values)
    return {key: values[min(len(values)-1, int((len(values)-1)*fraction))]
            for key, fraction in [('p50', .5), ('p95', .95), ('p99', .99), ('max', 1)]}


def test_concurrent_public_sdk_latency_and_complete_output(cluster):
    for worker in cluster.workers:
        cluster.connection.call('worker_update', identity=worker['id'], slots=8)
    samples = {'run_seconds': [], 'guest_seconds': [], 'read_seconds': [], 'status_seconds': []}
    with Pool(target='weave-live', image='docker://busybox:1.37.0', size=16, warm=16,
              memory=Memory('256MiB', '256MiB'), wait_timeout=900) as pool:
        async def run():
            leases = [pool.acquire() for _ in range(16)]
            environments = await asyncio.gather(*(lease.__aenter__() for lease in leases))
            try:
                async def episode(index, env):
                    value = f'agent-{index}'
                    await env.files.write_text.aio('/payload', value)
                    for _ in range(8):
                        started = time.perf_counter()
                        assert await env.files.read_text.aio('/payload') == value
                        samples['read_seconds'].append(time.perf_counter()-started)
                        started = time.perf_counter()
                        result = await env.run.aio('cat /payload')
                        samples['run_seconds'].append(time.perf_counter()-started)
                        samples['guest_seconds'].append(result.timings['guest_seconds'])
                        assert result.stdout == value
                    process = await env.exec.aio('true')
                    await process.wait.aio()
                    return process
                processes = await asyncio.gather(*(episode(i, env) for i, env in enumerate(environments)))
                (Path(os.environ['SANDWEAVE_WEAVE_INTEGRATION']) / 'commands.json').write_text(
                    json.dumps({key: summary(values) for key, values in samples.items() if values}, indent=2))
                async def status(i):
                    process = processes[i % len(processes)]
                    started = time.perf_counter()
                    # Bypass the SDK's completed-result cache to measure the RPC.
                    state = await process._acall('process_status')
                    samples['status_seconds'].append(time.perf_counter()-started)
                    assert state['returncode'] == 0
                await asyncio.wait_for(asyncio.gather(*(status(i) for i in range(512))), 30)
                process = await environments[0].exec.aio('head -c 3145728 /dev/zero', binary=True)
                await process.wait.aio()
                assert await process.stdout.read.aio() == bytes(3145728)
                assert (await process.result.aio(limit=0)).truncated['stdout']
            finally:
                await asyncio.gather(*(lease.__aexit__(None, None, None) for lease in leases))
        asyncio.run(run())
    report = {key: {'count': len(values), **summary(values)} for key, values in samples.items()}
    destination = Path(os.environ['SANDWEAVE_WEAVE_INTEGRATION']) / 'latency.json'
    destination.write_text(json.dumps(report, indent=2) + '\n')
    # Millisecond guest commands must not incur the reported tens-of-seconds
    # coordination delay, including a burst of 512 already-completed statuses.
    assert report['read_seconds']['p95'] < 1
    assert report['run_seconds']['p95'] < 2
    assert report['status_seconds']['p95'] < 3

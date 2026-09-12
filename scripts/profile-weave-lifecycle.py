#!/usr/bin/env python3
"""Measure controller claim concurrency against a delayed HTTP worker.

No sandbox is launched. Run with each SDK's Python/PYTHONPATH to compare the
same durable claim boundary and wire protocol; pair with live Pool tests.
"""
import argparse
import asyncio
import json
from pathlib import Path
import socket
import time

from aiohttp import web
from sandweave.sandbox.async_connection import AsyncConnection
from sandweave.sandbox.sandbox import definition
from sandweave.sandbox.wire import decode, encode
from sandweave.weave.controller import Controller
from sandweave.weave.dashboard import Dashboard
from sandweave.weave.pool import _claim
from sandweave.weave.server import RPC


async def listen(handler):
    app = web.Application()
    app.router.add_route('*', '/{path:.*}', handler)
    runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0, backlog=4096)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


async def run(args):
    root = Path(args.directory).resolve()
    root.mkdir(parents=True, exist_ok=False)
    entered = asyncio.Event()
    inflight = peak = 0
    async def worker(request):
        nonlocal inflight, peak
        message = decode(await request.read())
        assert message['op'] == 'managed_apply' and message['params']['action'] == 'claim'
        inflight += 1
        peak = max(peak, inflight)
        entered.set()
        try:
            await asyncio.sleep(args.delay)
            return web.Response(body=encode({'result': {'token': 'benchmark-token',
                'sandbox': {'id': message['params']['identity'], 'state': 'ready'}}}))
        finally:
            inflight -= 1
    worker_runner, port = await listen(worker)
    controller = Controller(root / 'controller')
    endpoint = dict(hostname=socket.gethostname(), port=port, token='benchmark-token', workspace='/benchmark')
    request = definition(detached=True)
    with controller.state.transaction():
        controller.state.put('pool', dict(id='pool-test', state='ready', desired='running', size=args.count,
            warm=0, request=request))
        for i in range(args.count):
            identity, lease = 'sw-' + str(i), 'lease-' + str(i)
            controller.state.put('allocation', dict(id=identity, parent='pool-test', state='claiming',
                desired='running', lease=lease, generation=2, released=False, endpoint=endpoint,
                spec=request['spec'], request=request))
            controller.state.put('lease', dict(id=lease, parent='pool-test', state='claiming',
                sandbox=identity, generation=2, owner=None))
    dashboard = Dashboard(controller, 'benchmark-controller')
    rpc = RPC(controller, 'benchmark-controller', dashboard, asyncio.Event())
    runner, port = await listen(rpc.handle)
    connection = AsyncConnection('127.0.0.1', port, 'benchmark-controller')
    poll_latencies = []
    done = asyncio.Event()
    async def poll(index):
        while not done.is_set():
            started = time.perf_counter()
            result = await connection.call('pool_lease', identity='pool-test', lease_id='lease-' + str(index))
            assert result['state'] in ('claiming', 'ready')
            poll_latencies.append(time.perf_counter() - started)
            await asyncio.sleep(.01)
    pollers = [asyncio.create_task(poll(i)) for i in range(min(args.count, 64))]
    try:
        started = time.perf_counter()
        for i in range(args.count):
            controller._submit(('allocation', 'sw-' + str(i)), _claim, controller,
                               'pool-test', 'lease-' + str(i), 'sw-' + str(i))
        await entered.wait()
        # Allow the initial transition batch to enter its worker wait.
        await asyncio.sleep(.1)
        probe_start = time.perf_counter()
        probe = controller.executor.submit(time.perf_counter)
        futures = list(controller.pending.values())
        await asyncio.gather(*(asyncio.wrap_future(future) for future in futures))
        elapsed = time.perf_counter() - started
        unrelated_wait = probe.result() - probe_start
        done.set()
        await asyncio.gather(*pollers)
        assert all(l['state'] == 'ready' for l in controller.state.list('lease'))
        values = sorted(poll_latencies)
        report = dict(kind='simulated delayed worker; real HTTP and durable controller claims',
            count=args.count, worker_delay_seconds=args.delay, peak_worker_requests=peak,
            claim_burst_seconds=elapsed, unrelated_executor_wait_seconds=unrelated_wait,
            polls=len(values), poll_seconds={name: values[int((len(values)-1)*fraction)]
                for name, fraction in [('p50', .5), ('p95', .95), ('p99', .99), ('max', 1)]})
        (root / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2), flush=True)
    finally:
        done.set()
        for task in pollers:
            task.cancel()
        await asyncio.gather(*pollers, return_exceptions=True)
        await connection.close()
        await runner.cleanup()
        await rpc.close()
        await asyncio.to_thread(dashboard.monitor.close)
        await asyncio.to_thread(controller.close)
        await worker_runner.cleanup()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True)
    parser.add_argument('--count', type=int, default=256)
    parser.add_argument('--delay', type=float, default=1)
    asyncio.run(run(parser.parse_args()))

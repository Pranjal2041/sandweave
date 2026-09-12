#!/usr/bin/env python3
"""Exercise HTTP + outbound relay with retained history and a simulated worker.

No guest is launched. Pair this coordination benchmark with
tests/integration/test_weave_latency_live.py for actual gVisor measurements.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from aiohttp import web
from sandweave.sandbox.async_connection import AsyncConnection
from sandweave.sandbox.sandbox import definition
from sandweave.sandbox.ownership import Owners
from sandweave.sandbox.wire import decode, encode
from sandweave.weave.state import State
from sandweave.weave.worker import Bridge


def percentiles(values):
    values = sorted(values)
    return {name: values[int((len(values)-1)*fraction)]
            for name, fraction in [('p50', .5), ('p95', .95), ('p99', .99), ('max', 1)]}


async def run(args):
    root = Path(args.directory).resolve()
    root.mkdir(parents=True, exist_ok=False)
    channel, credential = 'a'*32, 'benchmark-only'
    async def worker(request):
        message = decode(await request.read())
        params = message['params']
        if message['op'] == 'describe':
            result = {'id': params['identity'], 'state': 'ready', 'runtime_status': {'status': 'running'}}
        else:
            await asyncio.sleep(.001)
            result = params['payload']
        return web.Response(body=encode({'result': result}))
    app = web.Application()
    app.router.add_post('/rpc', worker)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0, backlog=4096)
    await site.start()
    endpoint = dict(hostname=socket.gethostname(), port=site._server.sockets[0].getsockname()[1],
                    token=credential, relay=channel)
    state = State(root / 'controller')
    owner = 'b'*32
    Owners(root / 'controller').register(owner, None)
    spec = definition(detached=True)['spec']
    with state.transaction():
        for i in range(args.history + 500):
            released = i >= 500
            identity = 'sample-' + str(i)
            state.put('allocation', dict(id=identity, state='terminated' if released else 'ready',
                desired='terminated' if released else 'running', generation=1, released=released,
                spec=spec, owner=None if released else owner, parent=None, worker=None, ack=True, deadline=None,
                token=credential, endpoint=endpoint,
                info={'id': identity, 'state': 'ready', 'runtime_status': {'status': 'running'}}))
    state.close()
    log = (root / 'controller.log').open('wb')
    child = subprocess.Popen([sys.executable, '-m', 'sandweave.weave.server', '--directory', str(root / 'controller')],
        env={**os.environ, **({'PYTHONPATH': args.controller_pythonpath} if args.controller_pythonpath else {})},
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    connection, bridge = None, None
    samples, resources, heartbeats = [], [], []
    try:
        marker = root / 'controller/controller.json'
        deadline = time.monotonic() + 30
        while not marker.exists():
            if child.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('benchmark controller did not start; inspect its log')
            await asyncio.sleep(.05)
        info = json.loads(marker.read_text())
        connection = AsyncConnection('127.0.0.1', info['port'], info['token'], timeout=30)
        bridge = Bridge({'url': f'http://127.0.0.1:{info["port"]}', 'token': info['token']}, channel).start()
        await connection.call('owner_register', identity=owner, process=None)
        async def renew():
            while True:
                started = time.perf_counter()
                await connection.call('owner_heartbeat', identity=owner)
                heartbeats.append(time.perf_counter()-started)
                raw = Path(f'/proc/{child.pid}/status').read_text().splitlines()
                resources.append({line.split(':')[0]: int(line.split()[1])
                                  for line in raw if line.startswith(('Threads:', 'VmRSS:'))})
                await asyncio.sleep(1)
        telemetry = asyncio.create_task(renew())
        started = time.perf_counter()
        async def client(index):
            sequence = 0
            while time.perf_counter() - started < args.seconds:
                payload = f'{index}:{sequence}'.encode()
                before = time.perf_counter()
                result = await connection.call('sandbox_rpc', identity='sample-'+str(index % 500),
                    method='echo', parameters={'payload': payload})
                assert result == payload
                samples.append(time.perf_counter()-before)
                sequence += 1
        try:
            await asyncio.gather(*(client(i) for i in range(args.concurrency)))
        finally:
            telemetry.cancel()
            await asyncio.gather(telemetry, return_exceptions=True)
        elapsed = time.perf_counter()-started
        report = dict(kind='simulated worker; real HTTP, relay, reconciliation and dashboard collection',
            history=args.history, active_records=500, concurrency=args.concurrency,
            requests=len(samples), seconds=elapsed, requests_per_second=len(samples)/elapsed,
            latency_seconds=percentiles(samples), heartbeat_seconds=percentiles(heartbeats),
            controller_max_threads=max(r['Threads'] for r in resources),
            controller_peak_rss_mib=max(r['VmRSS'] for r in resources)/1024)
        (root / 'results.json').write_text(json.dumps(report, indent=2)+'\n')
        print(json.dumps(report, indent=2))
    finally:
        if connection:
            await connection.call('shutdown')
            await connection.close()
        elif child.poll() is None:
            child.terminate()
        if bridge:
            await asyncio.to_thread(bridge.close)
        await asyncio.to_thread(child.wait, 30)
        log.close()
        await runner.cleanup()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, help='New directory for private benchmark state and results')
    parser.add_argument('--history', type=int, default=10000)
    parser.add_argument('--concurrency', type=int, default=1000)
    parser.add_argument('--seconds', type=float, default=30)
    parser.add_argument('--controller-pythonpath', help='Optional archived SDK source for a controller-only comparison')
    asyncio.run(run(parser.parse_args()))

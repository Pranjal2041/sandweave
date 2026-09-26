"""The controller must keep progressing independently of guest process count."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

import pytest


@pytest.fixture
def broker(monkeypatch):
    scripts = Path(__file__).parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('cpu_scale', scripts / 'cpu_broker.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_accounting_never_walks_guest_processes(broker, tmp_path, monkeypatch):
    cfg = {'root': os.getpid(), 'start': broker.process_table([os.getpid()])[os.getpid()]['start'],
           'cpus': [0], 'weight': 100, 'quota': None, 'state_path': str(tmp_path / 'state'),
           'control_path': str(tmp_path / 'control')}
    (tmp_path / 'state').write_text('{"status":"running"}')
    for index in range(36):
        (tmp_path / f'job-{index}.json').write_text(json.dumps(cfg))
    monkeypatch.setattr(broker, 'discover_trees', lambda *a: pytest.fail('guest process scan'))
    reads, calls = [], []
    original = broker.process_table
    def read(pids):
        pids = list(pids)
        reads.append(len(pids))
        return original(pids)
    monkeypatch.setattr(broker, 'process_table', read)
    async def call(connection, method, arg):
        calls.append((method, arg))
        return {'CPUTimeNS': 100, 'SampleAgeNS': 0, 'Runnable': 0,
                'Paused': arg.get('Paused', False), 'Disabled': False}
    monkeypatch.setattr(broker.ControlConnection, 'call', call)
    async def run():
        task = asyncio.create_task(broker.broker_loop(tmp_path, [0]))
        try:
            await asyncio.sleep(.6)
            report = json.loads((tmp_path / 'status.json').read_text())
            assert len(report['jobs']) == 36
            assert all(j['accounting'] == 'runtime' for j in report['jobs'].values())
            assert time.time() - report['time'] < .3
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
    assert max(reads) == 36  # Launcher identities, independent of descendants.
    assert len(calls) > 36 * 10
    assert sum(bool(arg.get('Disable')) for _,arg in calls) == 36


def test_one_stalled_runtime_does_not_delay_healthy_peer(broker, tmp_path, monkeypatch):
    counts = {'stalled': 0, 'healthy': 0}
    async def call(connection, method, arg):
        if arg.get('Disable'):
            return {}
        counts[connection.path] += 1
        if connection.path == 'stalled':
            await asyncio.sleep(.15)
            raise TimeoutError('unresponsive peer')
        return {'CPUTimeNS': counts['healthy']*1_000_000, 'SampleAgeNS': 0,
                'Runnable': 1, 'Paused': False, 'Disabled': False}
    monkeypatch.setattr(broker.ControlConnection, 'call', call)
    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            tasks = []
            for name in counts:
                path = tmp_path / name
                path.touch()
                job = broker.Job({'root': os.getpid(), 'weight': 100, 'control_path': name})
                job.ready = True
                tasks.append(asyncio.create_task(broker.serve_job(job, path, executor)))
            await asyncio.sleep(.4)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(run())
    assert counts['healthy'] >= 15
    assert counts['stalled'] <= 3


@pytest.mark.parametrize('directory', ['short', 'd' * 180])
def test_control_connection_reuses_stream_and_handles_split_reply(broker, tmp_path, directory):
    async def run():
        connections = []
        async def serve(reader, writer):
            connections.append(writer)
            try:
                for _ in range(2):
                    request = json.loads(await reader.read(4096))
                    assert request['method'] == 'containerManager.CPUControl'
                    writer.write(b'{"success":true,')
                    await writer.drain()
                    await asyncio.sleep(.01)
                    writer.write(b'"result":{"CPUTimeNS":123}}')
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        from _unix_sockets import Address
        parent = tmp_path / directory
        parent.mkdir()
        path = str(parent / 'control')
        with Address(path) as address:
            server = await asyncio.start_unix_server(serve, address)
        conn = broker.ControlConnection(path)
        try:
            for _ in range(2):
                assert await conn.call('CPUControl', {}) == {'CPUTimeNS': 123}
            assert len(connections) == 1
        finally:
            conn.close()
            server.close()
            await server.wait_closed()
    asyncio.run(run())


def test_paused_sample_does_not_lend_away_hungry_peers_share(broker):
    job = broker.Job({'root': 1, 'weight': 100})
    job.paused = True
    job.update_sample({'CPUTimeNS': 100, 'Runnable': 0, 'Paused': True}, 1.)
    assert job.demand is None


def test_aggregate_runnable_count_protects_cpu_starved_tasks(broker):
    job = broker.Job({'root': 1, 'weight': 100})
    for i in range(1, 20):
        job.update_sample({'CPUTimeNS': i*1_000_000, 'Runnable': 8, 'Paused': False}, i*.02)
    assert job.demand == 8


def test_helper_reaping_and_pid_reuse_do_not_recount_cpu(broker):
    job = broker.Job({'root': 1, 'weight': 100})
    def sample(start, cpu):
        return {9: {'start': start, 'self_cpu': cpu, 'cpu': cpu + 100}}
    job.update_helpers(sample(1, 2), {9: 1})
    job.update_helpers(sample(1, 3), {9: 1})
    assert job.helpers_cpu == 1
    job.update_helpers({}, {})
    job.update_helpers(sample(2, 5), {9: 1})
    job.update_helpers(sample(2, 5), {9: 2})
    job.update_helpers(sample(2, 6), {9: 2})
    assert job.helpers_cpu == 2


def test_legacy_demand_survives_until_next_sample(broker):
    job = broker.Job({'root': 1, 'start': 1, 'weight': 100})
    job.ready = True
    table = {1: {'parent': 0, 'start': 1, 'state': 'S', 'threads': 1, 'cpu': 0},
             2: {'parent': 1, 'start': 1, 'state': 'R', 'threads': 1, 'cpu': 1}}
    job.update(table, 1., interval=.25)
    # Otherwise demand expires before the next compatibility RPC and quota
    # pauses are discarded between every pair of samples.
    assert job.active_until > 1.25

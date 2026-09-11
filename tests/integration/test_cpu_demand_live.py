"""Demand sharing on a private four-CPU worker; no existing worker is changed."""
from contextlib import ExitStack
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from sandweave import CPU, Memory, Sandbox
from sandweave.sandbox.connection import Connection
from sandweave.sandbox.targets import Endpoint

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_CPU_INTEGRATION'), reason='explicit disposable CPU worker required')]


@dataclass
class Worker:
    target: Endpoint = field(repr=False)
    root: Path


@pytest.fixture(scope='module')
def worker():
    root = Path(os.environ['SANDWEAVE_CPU_INTEGRATION']).resolve()
    root.mkdir(parents=True, exist_ok=False)
    cpus = sorted(os.sched_getaffinity(0))[:4]
    assert len(cpus) == 4
    marker = root / 'worker.json'
    environment = {**os.environ, 'SANDWEAVE_HOME': str(root),
                   'SANDWEAVE_ASSETS': os.environ.get('SANDWEAVE_ASSETS', str(Path.cwd())),
                   'SANDWEAVE_MEMORY_BUDGET': '8GiB',
                   'PYTHONPATH': str(Path(__file__).resolve().parents[2] / 'src')}
    connection = None
    with (root / 'worker.log').open('w') as log:
        child = subprocess.Popen(['taskset', '-c', ','.join(map(str, cpus)), sys.executable,
            '-m', 'sandweave.sandbox.worker', '--metadata', str(marker)],
            env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True)
        try:
            deadline = time.monotonic() + 300
            while not marker.exists():
                assert child.poll() is None, (root / 'worker.log').read_text()[-5000:]
                assert time.monotonic() < deadline, 'test worker did not become ready'
                time.sleep(.1)
            info = json.loads(marker.read_text())
            connection = Connection('127.0.0.1', info['port'], info['token'])
            staged = Path(connection.call('ping')['workspace']) / 'scripts/cpu_broker.py'
            assert staged.read_bytes() == (Path(__file__).resolve().parents[2] / 'scripts/cpu_broker.py').read_bytes()
            yield Worker(Endpoint(info['port'], info['token']), root)
        finally:
            if connection is not None:
                for record in connection.call('list'):
                    if record['state'] not in ('terminated', 'stopped'):
                        connection.call('terminate', identity=record['id'])
                assert all(r['state'] in ('terminated', 'stopped') for r in connection.call('list'))
                connection.call('_shutdown_if_idle')
                connection.close()
            elif child.poll() is None:
                child.terminate()
            child.wait(timeout=30)


def sandbox(worker, *, weight=100, quota=None):
    return Sandbox(target=worker.target, cpu=CPU(vcpus=2, weight=weight, quota=quota),
                   memory=Memory('256MiB', '256MiB'))


def burn(env, count, *, intermittent=False):
    code = '''import os,time
for _ in range(COUNT):
 if os.fork()==0:
  while True:
   if INTERMITTENT:
    deadline=time.monotonic()+.003
    while time.monotonic()<deadline:pass
    time.sleep(.04)
   else:
    n=1
    for _ in range(20000):n=(n*1664525+1013904223)&0xffffffff
while True:time.sleep(1)
'''.replace('COUNT', str(count)).replace('INTERMITTENT', repr(intermittent))
    return env.exec(argv=['python', '-c', code])


def measure(envs, destination, *, seconds=8):
    root = Path(envs[0].status()['workspace'])
    local = Path((root / 'runs/local-path.txt').read_text().strip())
    keys = ['job-' + env.id + '.json' for env in envs]
    paths = list((local / 'gvisor/cpu-brokers').glob('*/status.json'))
    path = next(path for path in paths if all(key in json.loads(path.read_text())['jobs'] for key in keys))
    def sample():
        value = json.loads(path.read_text())
        assert time.time() - value['time'] < 3, 'CPU broker stopped reporting'
        # Include non-test CPU use: eligible CPUs need not be exclusively ours.
        cpus = {f'cpu{c}' for c in value['cpus']}
        counters = [list(map(int, fields[1:9])) for line in Path('/proc/stat').read_text().splitlines()
                    if (fields := line.split()) and fields[0] in cpus]
        return {'time': value['time'], 'jobs': [value['jobs'][key] for key in keys],
                'host_total': sum(sum(c) for c in counters),
                'host_idle': sum(c[3] + c[4] for c in counters)}
    time.sleep(1)
    samples = [sample()]
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(.25)
        samples.append(sample())
    elapsed = samples[-1]['time'] - samples[0]['time']
    rates = [(end['cpu_seconds'] - start['cpu_seconds']) / elapsed
             for start, end in zip(samples[0]['jobs'], samples[-1]['jobs'])]
    idle = (samples[-1]['host_idle'] - samples[0]['host_idle']) / (
        samples[-1]['host_total'] - samples[0]['host_total'])
    destination.write_text(json.dumps({'cpu_equivalents': rates, 'host_idle_fraction': idle,
                                     'samples': samples}, indent=2) + '\n')
    return rates, idle


@pytest.mark.parametrize('weight', [100, 300])
def test_partial_demand_borrows_and_then_reclaims_weight(worker, weight):
    with sandbox(worker, weight=weight) as first, sandbox(worker) as second:
        a, b = burn(first, 1), burn(second, 12)
        partial, idle = measure([first, second], worker.root / f'partial-{weight}.json')
        # There is no reserved physical core: host contention can reduce this
        # single process's delivered time. Verify progress and the borrower.
        assert .4 < partial[0] < 1.4, partial
        assert partial[1] > 2.5 and idle < .12, (partial, idle)
        a.terminate()
        a.wait(timeout=10)
        a = burn(first, 12)
        busy, idle = measure([first, second], worker.root / f'busy-{weight}.json')
        ratio = busy[0] / busy[1]
        assert .75 * weight / 100 < ratio < 1.35 * weight / 100, busy
        assert idle < .12, (busy, idle)
        a.terminate()
        b.terminate()


def test_borrowing_preserves_quota(worker):
    with sandbox(worker) as first, sandbox(worker, quota=1.5) as second:
        burn(first, 1)
        burn(second, 12)
        rates, _ = measure([first, second], worker.root / 'quota.json')
        assert .7 < rates[0] < 1.4, rates
        assert 1.15 < rates[1] < 1.8, rates


def test_busy_sandbox_borrows_from_fifteen_intermittent_peers(worker):
    with ExitStack() as stack:
        envs = [stack.enter_context(sandbox(worker)) for _ in range(16)]
        for env in envs[:-1]:
            burn(env, 1, intermittent=True)
        burn(envs[-1], 12)
        rates, idle = measure(envs, worker.root / 'sixteen-intermittent.json')
        assert rates[-1] > 2, rates  # Old equal-active policy allows only .25.
        assert idle < .15, (rates, idle)


def test_user_pause_releases_share_and_resume_reclaims_it(worker):
    with sandbox(worker, weight=300) as first, sandbox(worker) as second:
        burn(first, 12)
        burn(second, 12)
        first.pause()
        rates, idle = measure([first, second], worker.root / 'paused.json')
        assert rates[0] < .2 and rates[1] > 3, rates
        assert idle < .12, (rates, idle)
        first.resume()
        rates, idle = measure([first, second], worker.root / 'resumed.json')
        assert 2.25 < rates[0] / rates[1] < 4.05, rates
        assert idle < .12, (rates, idle)

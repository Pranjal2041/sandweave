"""Many simultaneous Weave starts on two disposable gVisor workers.

Set SANDWEAVE_WEAVE_SLOTS=8 (two workers with a 4GiB budget each). The
controller starts at most SANDWEAVE_MAX_STARTING=3 sandboxes per worker while
an unrelated sandbox keeps running commands.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import statistics
import threading
import time

import pytest

from sandweave import Memory, Sandbox
from sandweave.sandbox.connection import Connection
from test_weave_live import cluster, wait_for, pytestmark  # noqa: F401

MEMORY = Memory('256MiB', '256MiB')
LIMIT = 3
BURST = 14


@pytest.fixture(scope='module')
def limited():
    # The cluster fixture starts the controller after this, with this environment.
    previous = os.environ.get('SANDWEAVE_MAX_STARTING')
    os.environ['SANDWEAVE_MAX_STARTING'] = str(LIMIT)
    yield
    if previous is None:
        os.environ.pop('SANDWEAVE_MAX_STARTING', None)
    else:
        os.environ['SANDWEAVE_MAX_STARTING'] = previous


def record(name, value):
    path = os.environ.get('SANDWEAVE_SCALE_EVIDENCE')
    if path:
        evidence = json.loads(Path(path).read_text()) if Path(path).exists() else {}
        evidence[name] = value
        Path(path).write_text(json.dumps(evidence, indent=2, sort_keys=True) + '\n')


def timed(env):
    started = time.perf_counter()
    assert env.run('echo ok').stdout.strip() == 'ok'
    return time.perf_counter() - started


def test_simultaneous_starts_respect_the_worker_limit_and_unrelated_work_continues(limited, cluster):
    assert int(os.environ.get('SANDWEAVE_WEAVE_SLOTS', '2')) >= 8, 'set SANDWEAVE_WEAVE_SLOTS=8'
    unrelated = Sandbox(target='weave-live', memory=MEMORY, startup_timeout=900)
    try:
        baseline = [timed(unrelated) for _ in range(5)]
        stop, samples, during = threading.Event(), [], []

        def sample():
            while not stop.is_set():
                info = cluster.info
                starting = {}
                for allocation in info['sandboxes']:
                    if allocation['state'] == 'starting':
                        starting[allocation['worker']] = starting.get(allocation['worker'], 0) + 1
                samples.append({'starting': starting, 'workers': [w['state'] for w in info['workers']]})
                time.sleep(.2)

        def exercise():
            while not stop.is_set():
                during.append(timed(unrelated))

        observers = [threading.Thread(target=sample), threading.Thread(target=exercise)]
        for thread in observers:
            thread.start()
        started = time.perf_counter()
        try:
            with ThreadPoolExecutor(BURST) as threads:
                envs = list(threads.map(lambda _: Sandbox(target='weave-live', memory=MEMORY, startup_timeout=900),
                                        range(BURST)))
        finally:
            stop.set()
            for thread in observers:
                thread.join()
        burst = time.perf_counter() - started
        try:
            with ThreadPoolExecutor(BURST) as threads:
                assert list(threads.map(lambda env: env.run('echo ok').stdout.strip(), envs)) == ['ok'] * BURST
            peak = max((max(s['starting'].values(), default=0) for s in samples), default=0)
            assert peak <= LIMIT
            assert all(state == 'ready' for s in samples for state in s['workers'])
            assert len({a['id'] for a in cluster.info['sandboxes'] if not a['released']}) == BURST + 1

            # A sandbox whose runtime dies is still noticed within the 30 s settled observation interval.
            victim = envs[0]
            route = cluster.connection.call('allocation_route', identity=victim.id)['endpoint']
            worker = Connection('127.0.0.1', route['port'], route['token'])
            try:
                pid = worker.call('describe', identity=victim.id)['runtime_status']['sentry']['pid']
            finally:
                worker.close()
            os.kill(pid, signal.SIGKILL)
            killed = time.perf_counter()
            wait_for(lambda: next(a for a in cluster.info['sandboxes'] if a['id'] == victim.id)['state'] == 'failed',
                     timeout=60)
            detected = time.perf_counter() - killed
            record('weave_scale', {
                'workers': 2, 'worker_cpus': 2, 'start_limit': LIMIT, 'simultaneous_starts': BURST,
                'burst_seconds': burst, 'peak_starting_per_worker': peak, 'samples': len(samples),
                'unrelated_exec_baseline_median_seconds': statistics.median(baseline),
                'unrelated_exec_during_burst_median_seconds': statistics.median(during),
                'unrelated_exec_during_burst_max_seconds': max(during), 'unrelated_exec_calls_during_burst': len(during),
                'crash_detected_seconds': detected})
            assert detected < 40
        finally:
            for env in envs:
                env.close()
    finally:
        unrelated.close()

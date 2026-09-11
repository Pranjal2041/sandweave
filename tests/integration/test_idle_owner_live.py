"""Idle ownership using real workers, HTTP and a remote-client identity.

Only process identity is withheld, so local PID detection cannot hide lease
expiry. Heartbeat intervals, grace periods, RPCs and guests are unchanged.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

import pytest

from sandweave import Cluster, Memory, Pool, Sandbox
from sandweave.sandbox import ownership
from test_weave_live import cluster, wait_for, pytestmark


@pytest.mark.parametrize('warm', [0, 1])
def test_idle_pool_reacquires_and_renews_on_same_worker(cluster, monkeypatch, warm):
    monkeypatch.setattr(ownership, 'process_identity', lambda: None)
    peer = cluster.workers[1]['id']
    cluster.drain(peer)
    root = Path(os.environ['SANDWEAVE_WEAVE_INTEGRATION'])
    report = []
    try:
        with Cluster.connect(cluster.info['connection']['address'], token=cluster.connection.token) as remote:
            with Pool(target=remote, size=2, warm=warm, memory=Memory('128MiB', '256MiB')) as pool:
                with pool.acquire() as first:
                    assert first.run('echo first').stdout == 'first\n'
                    first_id = first.id
                wait_for(lambda: next(a for a in cluster.info['sandboxes'] if a['id'] == first_id)['released'])
                idle_since = time.monotonic()
                idle_seconds = ownership.GRACE_SECONDS + 10
                renewal_seconds = ownership.GRACE_SECONDS + 5
                time.sleep(idle_seconds)
                # Simultaneous returns share the new worker lease.
                def episode(index):
                    with pool.acquire() as env:
                        assert env.run('echo reacquired').stdout == 'reacquired\n'
                        time.sleep(renewal_seconds)  # Outlive another grace period.
                        assert env.run('echo still-alive').stdout == 'still-alive\n'
                        return {'id': env.id, 'state': env.status()['state']}
                with ThreadPoolExecutor(2) as executor:
                    report = list(executor.map(episode, range(2)))
                assert len({r['id'] for r in report}) == 2
                root.joinpath(f'idle-owner-warm-{warm}.json').write_text(json.dumps({
                    'first': first_id, 'idle_seconds': idle_seconds, 'renewal_seconds': renewal_seconds,
                    'elapsed_since_release': time.monotonic() - idle_since,
                    'reacquired': report}, indent=2) + '\n')
    finally:
        cluster.resume(peer)


def test_remote_creator_crash_still_cleans_managed_sandbox(cluster):
    root = Path(os.environ['SANDWEAVE_WEAVE_INTEGRATION'])
    code = '''import json,os,time
from sandweave import Cluster,Memory,Sandbox
from sandweave.sandbox import ownership
ownership.process_identity=lambda:None
settings=json.loads(os.environ['SANDWEAVE_TEST_CONNECTION'])
with Cluster.connect(**settings) as cluster:
 env=Sandbox(target=cluster,memory=Memory('128MiB','256MiB'))
 print(env.id,flush=True)
 time.sleep(300)
'''
    env_id = None
    with (root / 'crash-client.log').open('w') as log:
        child = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=log, text=True, start_new_session=True,
            env={**os.environ, 'SANDWEAVE_TEST_CONNECTION': json.dumps({
                'name': cluster.info['connection']['address'], 'token': cluster.connection.token})})
        try:
            assert select.select([child.stdout], [], [], 120)[0], 'test client did not publish a sandbox'
            env_id = child.stdout.readline().strip()
            assert env_id.startswith('sw-'), 'test client failed; inspect crash-client.log'
            print('Remote owner ready; checking recovery after forty seconds without heartbeats.', flush=True)
            # Leave renewal to the SDK, then suspend every client thread. A
            # remote worker cannot observe this client's local process state.
            time.sleep(ownership.HEARTBEAT_SECONDS * 2)
            child.send_signal(signal.SIGSTOP)
            time.sleep(40)  # Exceeds the former thirty-second timeout.
            with Sandbox.connect(env_id, target=cluster) as observer:
                assert observer.run('echo alive').stdout == 'alive\n'
            child.send_signal(signal.SIGCONT)
            time.sleep(ownership.HEARTBEAT_SECONDS * 2)
            with Sandbox.connect(env_id, target=cluster) as observer:
                assert observer.run('echo reconnected').stdout == 'reconnected\n'
            print('Missed-heartbeat recovery passed; checking the full grace period after client SIGKILL.', flush=True)
            child.kill()
            child.wait(timeout=10)
            killed_at = time.monotonic()
            time.sleep(ownership.GRACE_SECONDS - 30)
            with Sandbox.connect(env_id, target=cluster) as observer:
                assert observer.run('echo still-retained').stdout == 'still-retained\n'
            print('Sandbox still usable near the end of the grace period.', flush=True)
            wait_for(lambda: next(a for a in cluster.info['sandboxes'] if a['id'] == env_id)['released'],
                     timeout=45)
            records = [connection.call('describe', identity=env_id) for connection in cluster.test_workers
                       if any(r['id'] == env_id for r in connection.call('list'))]
            assert len(records) == 1
            assert records[0]['termination_reason'] == 'owner_heartbeat_expired'
            print('Remote ownership expired and the worker released the sandbox.', flush=True)
            root.joinpath('idle-owner-crash.json').write_text(json.dumps({
                'id': env_id, 'termination_reason': records[0]['termination_reason'],
                'state': records[0]['state'], 'missed_heartbeat_seconds': 40,
                'seconds_after_client_kill': time.monotonic() - killed_at,
                'grace_seconds': ownership.GRACE_SECONDS}, indent=2) + '\n')
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
            child.stdout.close()
            if env_id:
                cluster.connection.call('allocation_cancel', identity=env_id)

"""Runtime admission can be smaller than the unchanged runtime memory guard."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import time

import pytest

from sandweave import Memory, Pool, ResourceUnavailable, Sandbox
from sandweave.sandbox.process import Process
from sandweave.sandbox.resources import memory_bytes
from sandweave.sandbox.targets import Endpoint
from test_weave_live import cluster, wait_for

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_WEAVE_INTEGRATION'), reason='explicit disposable workers required')]


@pytest.fixture(scope='module', autouse=True)
def capacity():
    for name in ('SANDWEAVE_WEAVE_INTEGRATION', 'SANDWEAVE_LOCAL_DIR'):
        path = Path(os.environ.get(name, os.environ.get('TMPDIR', '/tmp')))
        path.mkdir(parents=True, exist_ok=True)
        disk = shutil.disk_usage(path)
        if disk.free - 8 * 1024**3 < disk.total * .15:
            pytest.skip('runtime sharing acceptance would leave less than 15% free')


def guard(env, cap):
    record = env.status()
    launch = json.loads((Path(record['workspace']) / 'runs/gvisor' / env.id / 'launch.json').read_text())
    assert '--runtime-memory-limit=' + str(memory_bytes(cap)) in launch
    assert env.info['memory']['runtime'] == cap
    return record


def receipt(name, data):
    root = Path(os.environ['SANDWEAVE_WEAVE_INTEGRATION'])
    (root / (name + '.json')).write_text(json.dumps(data, indent=2) + '\n')
    print(json.dumps(data))


@pytest.mark.parametrize('cluster', ['1GiB'], indirect=True)
def test_concurrent_runtime_only_sharing_preserves_guard_and_peer(cluster):
    target = Endpoint(cluster.test_workers[0].port, cluster.test_workers[0].token)
    with pytest.raises(ResourceUnavailable, match='memory budget exhausted'):
        Sandbox(target=target, memory=Memory('128MiB', '1GiB'))
    memory = Memory('128MiB', '1GiB', runtime_reservation='32MiB', experimental=True)
    latencies = []
    with Sandbox(target=target, memory=Memory('64MiB', '256MiB')) as control:
        with ExitStack() as stack, ThreadPoolExecutor(4) as executor:
            pending = [executor.submit(Sandbox, target=target, memory=memory) for _ in range(4)]
            # Harvest every successful creation before raising a failure, so
            # the ExitStack also cleans peers if one launch fails.
            envs, errors = [], []
            for future in pending:
                try:
                    envs.append(stack.enter_context(future.result()))
                except Exception as error:
                    errors.append(error)
            if errors:
                raise errors[0]
            records = [guard(e, '1GiB') for e in envs]
            assert sum(r['admission']['memory_reserved'] for r in records) == 640 * 1024**2
            with pytest.raises(ResourceUnavailable, match='memory budget exhausted'):
                Sandbox(target=target, memory=memory)
            # Concurrent commands exercise the running sandboxes while an
            # independent, fully reserved sandbox continues serving requests.
            work = [executor.submit(e.run, 'python -c "sum(range(10000000))"', timeout=30, check=True)
                    for e in envs]
            while not all(f.done() for f in work):
                disk = shutil.disk_usage(os.environ['SANDWEAVE_WEAVE_INTEGRATION'])
                assert disk.free > disk.total * .15
                before = time.monotonic()
                assert control.run('printf alive', timeout=10, check=True).stdout == 'alive'
                latencies.append(time.monotonic() - before)
            for future in work:
                future.result()
            envs[0].terminate()
            with Sandbox(target=target, memory=memory) as replacement:
                assert replacement.run('echo reused', check=True).stdout == 'reused\n'
                guard(replacement, '1GiB')
            assert all(e.run('printf alive', check=True).stdout == 'alive' for e in envs[1:])
    receipt('runtime-sharing-admission', {'worker_budget_mib': 1024, 'concurrent_sandboxes': 4,
        'guest_mib_each': 128, 'runtime_cap_mib_each': 1024, 'runtime_reservation_mib_each': 32,
        'total_reserved_including_control_mib': 960, 'fifth_rejected': True, 'reservation_reused': True,
        'peer_command_seconds': latencies})


@pytest.mark.parametrize('cluster', ['1GiB'], indirect=True)
def test_weave_pool_accounts_runtime_reservation(cluster):
    memory = Memory('128MiB', '1GiB', runtime_reservation='128MiB', experimental=True)
    with Pool(target='weave-live', size=4, warm=4, memory=memory, wait_timeout=180) as pool:
        with ExitStack() as stack, ThreadPoolExecutor(4) as executor:
            envs = [stack.enter_context(pool.acquire()) for _ in range(4)]
            records = [a for a in cluster.info['sandboxes'] if a['id'] in {e.id for e in envs}]
            assert len({r['worker'] for r in records}) == 2
            assert all(e.info['memory']['runtime_reservation'] == '128MiB' for e in envs)
            assert all(guard(e, '1GiB')['admission']['memory_reserved'] == 256 * 1024**2 for e in envs)
            assert list(executor.map(lambda e: e.run('echo pooled', check=True).stdout, envs)) == ['pooled\n'] * 4
            envs[0].files.write_text('/workspace/dirty', 'episode')
        with pool.acquire() as replacement:
            assert replacement.run('test ! -e /workspace/dirty').returncode == 0
            guard(replacement, '1GiB')
    wait_for(lambda: pool.info['state'] == 'closed')
    receipt('runtime-sharing-pool', {'workers': 2, 'worker_budget_mib': 1024, 'sandboxes': 4,
        'reserved_mib_each': 256, 'runtime_cap_mib_each': 1024, 'pristine_replacement': True})


@pytest.mark.parametrize('cluster', ['1GiB'], indirect=True)
@pytest.mark.parametrize('state', ['filesystem', 'memory'])
def test_snapshots_rebind_runtime_reservation_without_changing_cap(cluster, state):
    target = Endpoint(cluster.test_workers[0].port, cluster.test_workers[0].token)
    memory = Memory('256MiB', '512MiB', runtime_reservation='64MiB', experimental=True)
    with Sandbox(target=target, memory=memory) as env:
        env.files.write_text('/workspace/saved', 'runtime-sharing')
        if state == 'memory':
            process = env.exec(argv=['python', '-u', '-c',
                'a=bytearray(b"x")*(96*1024**2); print("ready"); '
                'input(); assert a[0]==a[-1]==120; print(len(a))'])
            assert process.stdout.readline() == 'ready\n'
        saved = env.snapshot(state=state)
        if state == 'memory':
            process.stdin.write('source\n')
            assert process.wait(timeout=30) == 0
    policies = [None, Memory('256MiB', '512MiB', runtime_reservation='32MiB', experimental=True),
                Memory('256MiB', '512MiB'), '256MiB']
    for policy in policies:
        with Sandbox(target=target, snapshot=saved, memory=policy) as restored:
            record = guard(restored, '512MiB')
            expected = '64MiB' if policy is None else getattr(policy, 'runtime_reservation', None)
            assert restored.info['memory'].get('runtime_reservation') == expected
            assert record['admission']['memory_reserved'] == memory_bytes('256MiB') + memory_bytes(expected or '512MiB')
            assert restored.files.read_text('/workspace/saved') == 'runtime-sharing'
            if state == 'memory':
                resumed = Process(restored, process.id)
                assert resumed.stdout.readline() == 'ready\n'
                resumed.stdin.write('restore\n')
                assert resumed.stdout.readline() == str(96 * 1024**2) + '\n'
                assert resumed.wait(timeout=30) == 0
    receipt('runtime-sharing-' + state, {'kind': state, 'restores': 4, 'runtime_cap_mib': 512,
        'runtime_reservations_mib': [64, 32, 512, 512], 'guest_files_preserved': True})

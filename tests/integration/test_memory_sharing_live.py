"""Reservation admission, real borrowing, guest limits and snapshot recovery."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
import time

import pytest

from sandweave import Memory, Pool, ResourceUnavailable, Sandbox
from sandweave.sandbox.process import Process
from sandweave.sandbox.targets import Endpoint
from test_weave_live import cluster, wait_for

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_WEAVE_INTEGRATION'), reason='explicit disposable workers required')]


def receipt(name, value):
    root = Path(os.environ['SANDWEAVE_WEAVE_INTEGRATION'])
    (root / (name + '.json')).write_text(json.dumps(value, indent=2) + '\n')


@pytest.mark.parametrize('cluster', ['32GiB'], indirect=True)
def test_four_sixteen_gib_guests_borrow_on_one_thirty_two_gib_worker(cluster):
    connection = cluster.test_workers[0]
    target = Endpoint(connection.port, connection.token)
    with Sandbox(target=target, memory='16GiB'):
        with pytest.raises(ResourceUnavailable, match='memory budget exhausted'):
            Sandbox(target=target, memory='16GiB')

    memory = Memory('16GiB', reservation='4GiB', experimental=True)
    with ExitStack() as stack, ThreadPoolExecutor(4) as executor:
        futures = [executor.submit(Sandbox, target=target, memory=memory) for _ in range(4)]
        envs, errors = [], []
        for future in futures:
            try:
                envs.append(stack.enter_context(future.result()))
            except Exception as error:
                errors.append(error)
        if errors:
            raise errors[0]
        records = [env.status() for env in envs]
        assert sum(r['admission']['memory_reserved'] for r in records) == 18 * 1024**3
        assert all(env.info['memory']['guest'] == '16GiB' for env in envs)
        program = ('import mmap; a=mmap.mmap(-1, 5*1024**3); '
                   'a[::4096]=b"x"*(len(a)//4096); print("borrowed", flush=True); '
                   'input(); assert a[::4096] == b"x"*(len(a)//4096)')
        borrower = envs[0].exec(argv=['python', '-u', '-c', program], timeout=120)
        assert borrower.stdout.readline() == 'borrowed\n'
        def ping(index):
            start = time.monotonic()
            assert envs[1 + index % 3].run('printf alive', timeout=30).stdout == 'alive'
            return time.monotonic() - start
        latencies = list(executor.map(ping, range(60)))
        borrower.stdin.write('release\n')
        assert borrower.wait(timeout=30) == 0
        # Another sandbox can allocate the same amount after the first frees it.
        assert envs[1].run(argv=['python', '-c', program.replace('input();', '')], timeout=120).returncode == 0
        receipt('four-on-32gib', {'guest_limits_gib': [16] * 4, 'reserved_gib': 18,
                                  'borrowed_guest_pages_gib': 5, 'peer_command_seconds': latencies,
                                  'second_borrower_passed': True})
    assert all(connection.call('describe', identity=e.id)['state'] == 'terminated' for e in envs)


@pytest.mark.parametrize('cluster', ['1GiB'], indirect=True)
def test_weave_pool_and_guest_limit_keep_other_sandboxes_running(cluster):
    memory = Memory('1GiB', '256MiB', reservation='128MiB', experimental=True)
    with Pool(target='weave-live', size=4, warm=4, memory=memory, wait_timeout=180) as pool:
        with ExitStack() as stack:
            envs = [stack.enter_context(pool.acquire()) for _ in range(4)]
            assert pool.info['active'] == 4
            records = [a for a in cluster.info['sandboxes'] if a['id'] in {e.id for e in envs}]
            assert len({r['worker'] for r in records}) == 2
            assert all(e.info['memory']['reservation'] == '128MiB' for e in envs)
            allocation = envs[0].run(argv=['python', '-c', 'a=bytearray(2*1024**3)'], timeout=30)
            assert allocation.returncode != 0, 'experimental admission must not enlarge the guest limit'
            with ThreadPoolExecutor(4) as executor:
                assert list(executor.map(lambda e: e.run('echo alive').stdout, envs)) == ['alive\n'] * 4
            envs[0].files.write_text('/workspace/dirty', 'episode')
        with pool.acquire() as replacement:
            assert replacement.run('test ! -e /workspace/dirty').returncode == 0
        receipt('pool-limits', {'workers': 2, 'worker_budget_gib': 1, 'concurrent_guests': 4,
                                'guest_limit_gib': 1, 'reservation_mib': 128, 'runtime_mib': 256,
                                'oversized_allocation_returncode': allocation.returncode,
                                'all_guests_survived': True, 'replacement_pristine': True})
    wait_for(lambda: pool.info['state'] == 'closed')


@pytest.mark.parametrize('cluster', ['1GiB'], indirect=True)
def test_memory_restore_can_change_admission_without_changing_saved_pages(cluster):
    connection = cluster.test_workers[0]
    target = Endpoint(connection.port, connection.token)
    memory = Memory('512MiB', '256MiB', reservation='128MiB', experimental=True)
    with Sandbox(target=target, memory=memory) as env:
        process = env.exec(argv=['python', '-u', '-c',
            'a=bytearray(b"x")*(192*1024**2); print("ready"); '
            'input(); assert a[0]==a[-1]==120; print(len(a))'])
        assert process.stdout.readline() == 'ready\n'
        saved = env.snapshot(state='memory')
        process.stdin.write('source\n')
        assert process.wait(timeout=30) == 0
    for policy in (memory, Memory('512MiB', '256MiB')):
        with Sandbox(target=target, snapshot=saved, memory=policy) as restored:
            restored_process = Process(restored, process.id)
            assert restored_process.stdout.readline() == 'ready\n'
            restored_process.stdin.write('restored\n')
            assert restored_process.stdout.readline() == str(192 * 1024**2) + '\n'
            assert restored_process.wait(timeout=30) == 0
            assert restored.info['memory'].get('reservation') == policy.reservation
    receipt('memory-restore', {'captured_guest_pages_mib': 192, 'shared_and_full_reservation_restores': True})

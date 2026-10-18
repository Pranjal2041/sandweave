"""CPU pause recovery when one application executor cannot acknowledge a stop."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import signal
import time

import pytest

from integration.test_cpu_demand_live import worker, sandbox
from integration.test_cpu_broker_scale_live import broker_status

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_CPU_INTEGRATION'), reason='disposable CPU worker required')]


@pytest.mark.parametrize('renew', [False, True])
def test_unacknowledged_pause_releases_guest_and_preserves_peer(worker, monkeypatch, renew):
    with sandbox(worker) as env, sandbox(worker) as peer:
        root, local, status = broker_status(env)
        monkeypatch.syspath_prepend(str(root / 'scripts'))
        spec = importlib.util.spec_from_file_location('pause_broker', root / 'scripts/cpu_broker.py')
        broker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(broker)
        key = 'job-' + env.id + '.json'
        registration = status.parent / key
        suspension = registration.with_name(registration.name + '.suspend')
        suspension.touch()
        process = None
        frozen = None
        broker_frozen = None
        try:
            # Let the broker release any existing pause before fault injection.
            time.sleep(.4)
            control = broker.ControlConnection(json.loads(registration.read_text())['control_path'])
            async def call(arg):
                try:
                    return await control.call('CPUControl', arg)
                finally:
                    control.close()
            asyncio.run(call({'Paused': False}))
            process = env.exec("python -c 'while True: pass'")
            state = json.loads((local / 'gvisor/state' / f'{env.id}_sandbox:{env.id}.state').read_text())
            sentry = state['sandbox']['pid']
            deadline = time.monotonic() + 10
            while frozen is None:
                before = broker.discover_trees([sentry])
                time.sleep(.15)
                after = broker.process_table(before)
                busy = [pid for pid, row in after.items()
                        if pid != sentry and row['start'] == before[pid]['start']
                        and row['self_cpu'] - before[pid]['self_cpu'] > .05
                        and Path(f'/proc/{pid}/cmdline').read_bytes() == b'']
                if busy:
                    frozen = max(busy, key=lambda pid: after[pid]['self_cpu'] - before[pid]['self_cpu'])
                    assert str(Path(f'/proc/{frozen}/exe').readlink()).endswith('/gvisor_sentry')
                    # Stop this test worker's broker briefly so its normal
                    # resume requests cannot cancel the injected pause. Sentry
                    # and the peer remain running; cleanup resumes the broker.
                    broker_frozen = json.loads(status.read_text())['pid']
                    os.kill(broker_frozen, signal.SIGSTOP)
                    os.kill(frozen, signal.SIGSTOP)
                assert time.monotonic() < deadline, 'no busy owned application executor found'
            # A host-stopped executor cannot acknowledge the runtime's pause.
            # The old engine times out here while holding its resume lock.
            started = time.monotonic()
            initial = asyncio.run(call({'Paused': True}))
            request_seconds = time.monotonic() - started
            assert initial['Paused'] and not initial['Disabled'], initial
            peer_latencies = []
            if renew:
                sample = initial
                while not sample['Disabled']:
                    tick = time.monotonic()
                    assert peer.run('true', timeout=2).returncode == 0
                    peer_latencies.append(time.monotonic() - tick)
                    time.sleep(.02)
                    sample = asyncio.run(call({'Paused': True}))
                    assert time.monotonic() - started < 2, sample
            else:
                time.sleep(.7)  # No broker renewal; the runtime timer must act.
                sample = asyncio.run(call({}))
            assert sample['Disabled'] and not sample['Paused'], sample
            assert 'did not quiesce' in sample['Reason']
            assert not asyncio.run(call({'Paused': True}))['Paused']  # Fenced.
            assert env.run('echo survived', timeout=2).stdout == 'survived\n'
            assert peer.run('echo peer', timeout=2).stdout == 'peer\n'
            result = {'renew': renew, 'pause_request_seconds': request_seconds,
                      'recovery_seconds': time.monotonic() - started,
                      'peer_max_seconds': max(peer_latencies, default=0),
                      'disabled_reason': sample['Reason'], 'both_guests_survived': True}
            os.kill(frozen, signal.SIGCONT)
            frozen = None
            os.kill(broker_frozen, signal.SIGCONT)
            broker_frozen = None
            deadline = time.monotonic() + 6
            while env.info['cpu_control']['state'] != 'degraded':
                assert time.monotonic() < deadline, env.info['cpu_control']
                time.sleep(.1)
            assert 'did not quiesce' in env.info['cpu_control']['reason']
            (worker.root / f'pause-recovery-{renew}.json').write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result))
        finally:
            if frozen is not None:
                os.kill(frozen, signal.SIGCONT)
            # This also recovers the old-engine negative control after its
            # timed-out request, so the test never leaves a frozen process.
            try:
                broker.control_pause(json.loads(registration.read_text())['control_path'], False)
            finally:
                if broker_frozen is not None:
                    os.kill(broker_frozen, signal.SIGCONT)
            if process is not None:
                process.terminate()
            suspension.unlink(missing_ok=True)


def test_user_space_spinlocks_progress_under_quota(worker):
    with sandbox(worker, quota=.5) as env, sandbox(worker) as peer:
        env.files.write_text('/tmp/spin.c', r'''
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <unistd.h>
static atomic_flag lock = ATOMIC_FLAG_INIT;
static _Atomic unsigned long count;
static _Atomic int done;
static void *work(void *unused) {
  (void)unused;
  while (!atomic_load_explicit(&done, memory_order_relaxed)) {
    while (atomic_flag_test_and_set_explicit(&lock, memory_order_acquire)) {}
    for (volatile int i=0; i<10000; i++) {}
    atomic_fetch_add_explicit(&count, 1, memory_order_relaxed);
    atomic_flag_clear_explicit(&lock, memory_order_release);
  }
  return NULL;
}
int main(void) {
  pthread_t threads[8];
  for (int i=0; i<8; i++) if (pthread_create(&threads[i], NULL, work, NULL)) return 2;
  for (int i=0; i<5; i++) { sleep(1); printf("%lu\n", atomic_load(&count)); fflush(stdout); }
  atomic_store(&done, 1);
  for (int i=0; i<8; i++) pthread_join(threads[i], NULL);
  return 0;
}
''')
        env.run('gcc -O2 -pthread -Wall -Wextra -Werror /tmp/spin.c -o /tmp/spin', check=True)
        process = env.exec('/tmp/spin', timeout=30)
        deadline = time.monotonic() + 35
        while process.poll() is None:
            assert time.monotonic() < deadline, 'spinlock workload stopped making progress'
            assert peer.run('true', timeout=2).returncode == 0
            time.sleep(.1)
        result = process.result()
        counts = [int(value) for value in result.stdout.splitlines()]
        assert result.returncode == 0 and len(counts) == 5
        assert all(b > a for a, b in zip(counts, counts[1:])), counts
        _, _, path = broker_status(env)
        report = json.loads(path.read_text())['jobs']['job-' + env.id + '.json']
        assert report['stops'] > 0 and report['degraded'] is None, report
        (worker.root / 'spinlocks.json').write_text(json.dumps({'counts': counts, 'stops': report['stops']}) + '\n')

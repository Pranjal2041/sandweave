"""Process-heavy CPU sharing and controller failure, on a disposable worker.

SANDWEAVE_CPU_INTEGRATION must name an empty directory. Set
SANDWEAVE_CPU_TEST_MEMORY=32GiB for the 36-sandbox qualification.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import time

import pytest

from integration.test_cpu_demand_live import worker, sandbox, burn, measure

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_CPU_INTEGRATION'), reason='explicit disposable CPU worker required')]


def broker_status(env):
    root = Path(env.status()['workspace'])
    local = Path((root / 'runs/local-path.txt').read_text().strip())
    key = 'job-' + env.id + '.json'
    path = next(p for p in (local / 'gvisor/cpu-brokers').glob('*/status.json')
                if key in json.loads(p.read_text())['jobs'])
    return root, local, path


def proc_cpu(pid):
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK')


def test_many_sleeping_processes_do_not_consume_the_broker(worker):
    count = int(os.environ.get('SANDWEAVE_CPU_SCALE_SANDBOXES', '36'))
    sleepers = int(os.environ.get('SANDWEAVE_CPU_SCALE_SLEEPERS', '240'))
    # Native sleepers avoid the per-process Python heap becoming a guest-memory
    # test. Compile inside the guest so the executable matches its ABI.
    code = '''#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
 if (argc != 2) return 2;
 for (int i=0; i<atoi(argv[1]); i++) {
  pid_t pid=fork();
  if (pid<0) { perror("fork"); return 1; }
  if (pid==0) { for (;;) pause(); }
 }
 puts("READY"); fflush(stdout);
 for (;;) pause();
}
'''
    with ExitStack() as stack:
        envs = [stack.enter_context(sandbox(worker)) for _ in range(count)]
        root, local, path = broker_status(envs[0])
        broker_pid = json.loads(path.read_text())['pid']
        baseline_start, baseline_cpu = time.monotonic(), proc_cpu(broker_pid)
        time.sleep(3)
        baseline_rate = (proc_cpu(broker_pid)-baseline_cpu)/(time.monotonic()-baseline_start)
        envs[0].files.write_text('/tmp/sleepers.c', code)
        envs[0].run('gcc -O2 -Wall -Wextra -Werror /tmp/sleepers.c -o /tmp/sleepers', check=True)
        binary = envs[0].files.read_bytes('/tmp/sleepers')
        def start(env):
            env.files.write_bytes('/tmp/sleepers', binary, mode=0o755)
            process = env.exec(argv=['/tmp/sleepers', str(sleepers)])
            line = process.stdout.readline()
            assert line.strip() == 'READY', process.result()
            return process
        with ThreadPoolExecutor(max_workers=8) as executor:
            processes = list(executor.map(start, envs))
        # Count owned host processes once, outside the controller hot path.
        module = importlib.util.spec_from_file_location('owned_proc', root / 'scripts/cpu_broker.py')
        inspector = importlib.util.module_from_spec(module)
        module.loader.exec_module(inspector)
        registrations = [json.loads(p.read_text()) for p in path.parent.glob('job-*.json')]
        owned = inspector.discover_trees([r['root'] for r in registrations])
        assert len(owned) >= count * sleepers, len(owned)
        time.sleep(1)
        started, before = time.monotonic(), proc_cpu(broker_pid)
        ages, latencies = [], []
        for index in range(80):
            report = json.loads(path.read_text())
            ages.append(time.time()-report['time'])
            assert len(report['jobs']) == count
            assert all(j['accounting'] == 'runtime' and j['degraded'] is None
                       for j in report['jobs'].values()), report
            tick = time.monotonic()
            assert envs[index % count].run('true').returncode == 0
            latencies.append(time.monotonic()-tick)
            time.sleep(.05)
        broker_rate = (proc_cpu(broker_pid)-before)/(time.monotonic()-started)
        # A few busy sandboxes must still borrow idle capacity amid thousands
        # of sleeping stubs. The existing pair suite checks unequal weights.
        a, b = burn(envs[0], 12), burn(envs[1], 12)
        time.sleep(1)
        registrations = [json.loads(p.read_text()) for p in path.parent.glob('job-*.json')]
        # Independent host counters, sampled only at the boundaries. This
        # catches aggregate-clock undercounting as well as policy regressions.
        before_table = inspector.discover_trees([r['root'] for r in registrations])
        measured_at = time.monotonic()
        rates, idle = measure(envs, worker.root / 'scale-fairness.json')
        after_table = inspector.process_table(before_table)
        independent = sum(max(0., p['self_cpu']-before_table[pid]['self_cpu'])
                          for pid,p in after_table.items()
                          if p['start'] == before_table[pid]['start']) / (time.monotonic()-measured_at)
        result = {'sandboxes': count, 'sleepers_per_guest': sleepers,
                  'host_processes': len(owned), 'broker_idle_cpu': baseline_rate,
                  'broker_process_heavy_cpu': broker_rate,
                  'max_status_age_seconds': max(ages),
                  'command_median_seconds': statistics.median(latencies),
                  'command_max_seconds': max(latencies), 'busy_cpu_rates': rates,
                  'host_idle_fraction': idle, 'independent_host_cpu': independent}
        (worker.root / 'scale.json').write_text(json.dumps(result, indent=2)+'\n')
        assert broker_rate < .35, result
        assert max(ages) < 1., result
        assert sum(rates) > 3 and .7 < rates[0]/rates[1] < 1.4, result
        assert abs(sum(rates)-independent) < .5, result
        a.terminate()
        b.terminate()


@pytest.mark.parametrize('failure', [signal.SIGSTOP, signal.SIGKILL])
def test_failed_broker_releases_pauses_without_killing_guests(worker, failure):
    with sandbox(worker, quota=.1) as first, sandbox(worker) as second:
        burn(first, 4)
        _, _, path = broker_status(first)
        key = 'job-' + first.id + '.json'
        deadline = time.monotonic()+10
        while True:
            report = json.loads(path.read_text())
            if report['jobs'][key]['paused']:
                break
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert set(report['jobs']) == {key, 'job-' + second.id + '.json'}
        pid = report['pid']
        os.kill(pid, failure)
        try:
            started = time.monotonic()
            assert first.run('echo survived', timeout=5).stdout.strip() == 'survived'
            recovery = time.monotonic()-started
            assert second.run('echo peer', timeout=5).stdout.strip() == 'peer'
            time.sleep(4)
            assert first.info['cpu_control']['state'] == 'degraded'
            assert second.info['cpu_control']['state'] == 'degraded'
        finally:
            if failure == signal.SIGSTOP:
                os.kill(pid, signal.SIGCONT)
        # A late broker request must not reapply a revoked pause.
        time.sleep(.5)
        assert first.run('echo still-running', timeout=5).returncode == 0
        first.pause()
        first.resume()
        assert first.run('true', timeout=5).returncode == 0
        (worker.root/f'broker-failure-{failure.name}.json').write_text(json.dumps({
            'recovery_seconds': recovery, 'both_survived': True,
            'late_requests_fenced': True, 'user_pause_resume': True}, indent=2)+'\n')


def test_unresponsive_runtime_does_not_stall_other_sandboxes(worker):
    with sandbox(worker, quota=.1) as first, sandbox(worker) as second:
        burn(first, 4)
        root, local, path = broker_status(first)
        state = json.loads((local/'gvisor/state'/f'{first.id}_sandbox:{first.id}.state').read_text())
        sentry = state['sandbox']['pid']
        os.kill(sentry, signal.SIGSTOP)
        latencies, ages = [], []
        try:
            deadline = time.monotonic()+5
            while time.monotonic() < deadline:
                started = time.monotonic()
                assert second.run('true', timeout=2).returncode == 0
                latencies.append(time.monotonic()-started)
                ages.append(time.time()-json.loads(path.read_text())['time'])
                time.sleep(.05)
            assert (root/'runs/gvisor'/first.id/'cpu-controller-degraded.txt').exists()
        finally:
            os.kill(sentry, signal.SIGCONT)
        assert first.run('echo survived', timeout=5).stdout.strip() == 'survived'
        assert first.info['cpu_control']['state'] == 'degraded'
        assert max(latencies) < 1 and max(ages) < 1
        (worker.root/'peer-failure.json').write_text(json.dumps({
            'max_command_seconds': max(latencies), 'max_status_age_seconds': max(ages),
            'both_survived': True}, indent=2)+'\n')


@pytest.mark.skipif(not os.environ.get('SANDWEAVE_CPU_LEGACY_ASSETS'), reason='older pinned engine required')
def test_older_pinned_engine_uses_isolated_compatibility_accounting(worker):
    from sandweave.sandbox.workspace import stage_tree
    old = Path(os.environ['SANDWEAVE_CPU_LEGACY_ASSETS'])
    descriptor = json.loads((old/'tools/gvisor-socket/runtime.json').read_text())
    assert descriptor.get('cpu_accounting', 0) == 0
    with sandbox(worker) as peer:
        root, _, path = broker_status(peer)
        stage_tree(old/descriptor['path'], root/descriptor['path'])
        name = 'legacy-cpu-test'
        def manager(code):
            result = subprocess.run([sys.executable, '-c',
                'import sys,json;sys.path.insert(0,sys.argv[1]);'
                'from environment import EnvironmentManager;'
                'm=EnvironmentManager(sys.argv[2]);' + code,
                str(root/'scripts'), str(root)], text=True, capture_output=True, timeout=30)
            assert result.returncode == 0, result.stderr
            return result.stdout
        options = ['--runtime-build', descriptor['path'], '--guest-cpus', '2',
                   '--memory-mib', '256', '--runtime-memory-mib', '256', '--guest-gs',
                   '--cpu-policy', 'quota', '--cpu-quota', '.1']
        try:
            manager(f'm.start({name!r}, command=["python3","-c","while True: pass"], options={options!r})')
            deadline = time.monotonic()+15
            key = 'job-'+name+'.json'
            while True:
                report = json.loads(path.read_text())
                legacy = report['jobs'].get(key, {})
                if legacy.get('stops', 0) > 1:
                    break
                assert time.monotonic() < deadline, report
                assert peer.run('true', timeout=2).returncode == 0
                time.sleep(.1)
            assert legacy['accounting'] == 'legacy' and legacy['degraded'] is None
            assert set(report['jobs']) == {key, 'job-'+peer.id+'.json'}
            os.kill(report['pid'], signal.SIGKILL)
            time.sleep(5)
            assert peer.run('true', timeout=2).returncode == 0
            assert manager(f'print(m._run([*m._command({name!r}),"exec",{name!r},"/bin/echo","survived"]))').strip() == 'survived'
            (worker.root/'legacy.json').write_text(json.dumps({
                'runtime': descriptor['path'], 'accounting': legacy['accounting'],
                'quota_pauses': legacy['stops'], 'both_survived_broker_death': True}, indent=2)+'\n')
        finally:
            manager(f'm.stop({name!r}, discard=True)')

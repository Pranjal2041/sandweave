"""The optional gVisor KVM platform on a worker that provides /dev/kvm."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import pytest

from sandweave import Memory, Sandbox

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit isolated worker required'),
    pytest.mark.skipif(not os.access('/dev/kvm', os.R_OK | os.W_OK), reason='this host does not provide /dev/kvm')]
MEMORY = Memory('1GiB', '512MiB')
PROBE = r'''
import json, os, time
def rate(n, f):
    best = None
    for _ in range(5):
        t = time.perf_counter()
        for _ in range(n): f()
        value = (time.perf_counter() - t) / n * 1e9
        best = value if best is None else min(best, value)
    return best
print(json.dumps({'getppid_ns': rate(50000, os.getppid), 'stat_ns': rate(20000, lambda: os.stat('/tmp')),
                  'open_close_ns': rate(5000, lambda: os.close(os.open('/etc/hostname', os.O_RDONLY)))}))
'''
COUNTER = 'i=0; while true; do i=$((i+1)); echo $i > /tmp/count; sleep 0.1; done'


def record(name, value):
    path = os.environ.get('SANDWEAVE_KVM_EVIDENCE')
    if path:
        evidence = json.loads(Path(path).read_text()) if Path(path).exists() else {}
        Path(path).write_text(json.dumps({**evidence, name: value}, indent=2, sort_keys=True) + '\n')


def probe(env):
    env.files.write_text('/tmp/probe.py', PROBE)
    return json.loads(env.run('python3 /tmp/probe.py', timeout=300).stdout)


def gs_probe():
    source = Path(__file__).resolve().parents[2] / 'sources/gs-preserve-test.c'
    output = Path(tempfile.mkdtemp(prefix='sw-gs-')) / 'gs'
    subprocess.run(['cc', '-O2', '-static', '-nostdlib', '-fno-stack-protector', '-o', str(output), str(source)],
                   check=True)
    return output.read_bytes()


def counting(env):
    first = int(env.files.read_text('/tmp/count'))
    time.sleep(1)
    return int(env.files.read_text('/tmp/count')) > first


def test_kvm_runs_commands_files_network_and_pause_faster_for_system_calls():
    timings = {}
    for label, option in (('systrap', {}), ('kvm', {'kvm': True})):
        started = time.perf_counter()
        with Sandbox(memory=MEMORY, startup_timeout=600, **option) as env:
            timings[label] = {'startup_seconds': time.perf_counter() - started, **probe(env)}
            assert env.info['kvm'] is (label == 'kvm')
            env.files.write_text('/workspace/value', label)
            assert env.run('cat /workspace/value').stdout == label
            assert env.run("python3 -c \"import socket; print(socket.gethostbyname('example.com'))\"",
                           timeout=60).returncode == 0
            env.pause()
            env.resume()
            assert env.run('echo resumed').stdout.strip() == 'resumed'
            durations = []
            for _ in range(20):
                t = time.perf_counter()
                env.run('true')
                durations.append(time.perf_counter() - t)
            timings[label]['command_round_trip_ms'] = sorted(durations)[len(durations) // 2] * 1e3
    record('platforms', timings)
    assert timings['kvm']['getppid_ns'] * 5 < timings['systrap']['getppid_ns']


def test_auto_uses_kvm_here_and_restores_keep_or_change_the_platform():
    with Sandbox(memory=MEMORY, startup_timeout=600, kvm='auto') as env:
        assert env.info['kvm'] is True
        env.files.write_text('/workspace/saved', 'kept')
        env.exec(COUNTER)
        time.sleep(1)
        memory = env.snapshot(state='memory')
        filesystem = env.snapshot(state='filesystem')
    results = {}
    for name, options in (('inherited', {}), ('without_kvm', {'kvm': False})):
        started = time.perf_counter()
        with Sandbox(snapshot=memory.id, startup_timeout=600, **options) as restored:
            results[name] = {'restore_seconds': time.perf_counter() - started, 'kvm': restored.info['kvm']}
            assert restored.files.read_text('/workspace/saved') == 'kept'
            assert counting(restored), 'the saved process stopped'
            # A sandbox restored without KVM records that platform for its own snapshots.
            if name == 'without_kvm':
                again = restored.snapshot(state='memory')
    assert results['inherited']['kvm'] is True and results['without_kvm']['kvm'] is False
    with Sandbox(snapshot=again.id, startup_timeout=600) as restored:
        assert restored.info['kvm'] is False and counting(restored)
    with Sandbox(snapshot=filesystem.id, startup_timeout=600) as restored:
        assert restored.info['kvm'] is True
        assert restored.files.read_text('/workspace/saved') == 'kept'
    record('restores', results)


def test_guest_clock_and_application_gs_on_kvm():
    offset = -100 * 86400
    with Sandbox(memory=MEMORY, startup_timeout=600, kvm=True, clock_offset=offset) as env:
        assert env.info['kvm'] is True
        guest = json.loads(env.run('python3 -c "import json,time; print(json.dumps([time.time(), '
                                   'time.clock_gettime(time.CLOCK_MONOTONIC)]))"').stdout)
        assert abs(guest[0] - (time.time() + offset)) < 30
        # The fast path (vDSO) and the system call agree.
        date = int(env.run('date +%s').stdout)
        assert abs(date - guest[0]) < 30
    # Programs that keep their own GS base, as the default platform's --guest-gs already protects.
    binary = gs_probe()
    for option in ({}, {'kvm': True}):
        with Sandbox(memory=MEMORY, startup_timeout=600, **option) as env:
            env.files.write_bytes('/tmp/gs', binary)
            result = env.run('chmod +x /tmp/gs && /tmp/gs')
            assert (result.returncode, result.stdout) == (0, 'gs ok\n'), result
    record('application_gs', 'preserved across 1,000 system calls on both platforms')


def test_concurrent_kvm_starts():
    count = 12
    started = time.perf_counter()
    with ThreadPoolExecutor(count) as threads:
        envs = list(threads.map(lambda _: Sandbox(memory=Memory('512MiB', '256MiB'), startup_timeout=900, kvm=True),
                                range(count)))
    try:
        ready = time.perf_counter() - started
        with ThreadPoolExecutor(count) as threads:
            assert list(threads.map(lambda env: env.run('echo ok').stdout.strip(), envs)) == ['ok'] * count
        assert all(env.info['kvm'] for env in envs)
        record('concurrent', {'sandboxes': count, 'all_ready_seconds': ready})
    finally:
        for env in envs:
            env.close()

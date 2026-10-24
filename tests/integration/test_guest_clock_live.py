"""Guest wall-clock offsets, start dates and frozen pauses using the public SDK."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import statistics
import threading
import time

import pytest

from sandweave import CPU, Memory, Pool, Sandbox, SandboxError
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit isolated worker required')]
MEMORY = Memory('512MiB', '512MiB')
DAY = 86400
# Commands, not clocks, dominate these bounds: each probe is one RPC.
SLACK = 1.0
EVIDENCE = {}

PROBE = r'''
import ctypes, json, os, time
libc = ctypes.CDLL(None, use_errno=True)
class timespec(ctypes.Structure):
    _fields_ = [('s', ctypes.c_long), ('ns', ctypes.c_long)]
class timeval(ctypes.Structure):
    _fields_ = [('s', ctypes.c_long), ('us', ctypes.c_long)]
def syscall_clock(clock):
    value = timespec()
    assert libc.syscall(228, clock, ctypes.byref(value)) == 0, ctypes.get_errno()
    return value.s + value.ns / 1e9
def libc_clock(clock):
    value = timespec()
    assert libc.clock_gettime(clock, ctypes.byref(value)) == 0
    return value.s + value.ns / 1e9
tv = timeval()
assert libc.gettimeofday(ctypes.byref(tv), None) == 0
result = {
    'vdso_realtime': libc_clock(0), 'python_time': time.time(),
    'gettimeofday': tv.s + tv.us / 1e6, 'time_syscall': libc.syscall(201, None),
    'syscall_realtime': syscall_clock(0), 'syscall_realtime_coarse': syscall_clock(5),
    'vdso_monotonic': libc_clock(1), 'syscall_monotonic': syscall_clock(1),
    'syscall_boottime': syscall_clock(7),
}
for name, path in (('tmp_file', '/tmp/clock-probe'), ('root_file', '/root/clock-probe')):
    with open(path, 'w') as stream:
        stream.write('x')
    result[name] = os.stat(path).st_mtime
result['uptime'] = float(open('/proc/uptime').read().split()[0])
result['btime'] = int(next(line for line in open('/proc/stat') if line.startswith('btime')).split()[1])
start = time.perf_counter()
for _ in range(200000):
    time.time()
result['vdso_calls_per_second'] = 200000 / (time.perf_counter() - start)
start = time.perf_counter()
for _ in range(20000):
    syscall_clock(0)
result['syscall_calls_per_second'] = 20000 / (time.perf_counter() - start)
print(json.dumps(result))
'''
REALTIME = ('vdso_realtime', 'python_time', 'gettimeofday', 'syscall_realtime', 'syscall_realtime_coarse',
            'tmp_file', 'root_file')


def record(name, value):
    EVIDENCE[name] = value
    path = os.environ.get('SANDWEAVE_CLOCK_EVIDENCE')
    if path:
        Path(path).write_text(json.dumps(EVIDENCE, indent=2, sort_keys=True) + '\n')


@pytest.fixture(scope='module', autouse=True)
def stop_idle_worker():
    yield
    connection = local_connection()
    deadline = time.monotonic() + 15
    try:
        while True:
            try:
                connection.call('_shutdown_if_idle')
                break
            except SandboxError as error:
                if str(error) != 'worker still owns live sandboxes' or time.monotonic() > deadline:
                    raise
                time.sleep(.1)
    finally:
        connection.close()


def probe(env):
    """Return guest clock readings with the host realtime interval around them."""
    env.files.write_text('/tmp/clock_probe.py', PROBE)
    before = time.time()
    result = env.run('python3 /tmp/clock_probe.py', check=True)
    after = time.time()
    return {**json.loads(result.stdout), 'host_before': before, 'host_after': after}


def assert_offset(reading, offset):
    """Every guest realtime source reads host realtime plus the offset."""
    low, high = reading['host_before'] + offset - SLACK, reading['host_after'] + offset + SLACK
    for name in REALTIME:
        assert low <= reading[name] <= high, (name, reading[name] - reading['host_before'], offset)
    # time(2) truncates to seconds.
    assert low - 1 <= reading['time_syscall'] <= high, reading['time_syscall'] - reading['host_before']
    # Linux derives boot time from realtime less boot-based monotonic time.
    assert abs(reading['vdso_monotonic'] - reading['uptime']) < SLACK
    assert abs(reading['syscall_boottime'] - reading['syscall_monotonic']) < SLACK
    assert abs(reading['btime'] - (reading['syscall_realtime'] - reading['syscall_monotonic'])) <= 2
    # The VDSO, not the syscall fallback, served Python's time.time().
    assert reading['vdso_calls_per_second'] > 5 * reading['syscall_calls_per_second'], reading


def test_offset_covers_syscalls_vdso_files_and_proc():
    offset = -400 * DAY + 0.25
    with Sandbox(clock_offset=offset, memory=MEMORY) as env:
        reading = probe(env)
        assert_offset(reading, offset)
        # A fresh sandbox's monotonic clock starts near zero; offsets leave it alone.
        assert 0 < reading['vdso_monotonic'] < 120
        assert env.info['clock'] == {'offset_seconds': offset, 'pause': 'freeze'}
        date = env.run('date -u +%Y-%m-%d', check=True).stdout.strip()
        assert date == (datetime.now(timezone.utc) + timedelta(seconds=offset)).strftime('%Y-%m-%d')
        record('offset', {key: reading[key] - reading['host_before'] for key in REALTIME} | {
            'vdso_calls_per_second': reading['vdso_calls_per_second'],
            'syscall_calls_per_second': reading['syscall_calls_per_second']})


def test_start_date_and_default_host_time():
    start = datetime(2025, 3, 1, tzinfo=timezone.utc)
    created = time.time()
    with Sandbox(clock_start=start, memory=MEMORY) as env:
        reading = probe(env)
        elapsed = reading['syscall_realtime'] - start.timestamp()
        assert 0 <= elapsed <= reading['host_after'] - created + SLACK
        assert_offset(reading, reading['syscall_realtime'] - (reading['host_before'] + reading['host_after']) / 2)
        assert env.info['clock']['start'] == '2025-03-01T00:00:00+00:00'
    with Sandbox(memory=MEMORY) as env:
        assert_offset(probe(env), 0)
        assert 'clock' not in env.info


def measure_pause(env, pause_seconds):
    """Return guest realtime/monotonic advance across a pause and a sleeper's view."""
    sleeper = env.exec("python3 -c 'import time; m, r = time.monotonic(), time.time(); time.sleep(3); "
                       "print(time.monotonic() - m, time.time() - r)'")
    time.sleep(1)
    first = probe(env)
    env.pause()
    time.sleep(pause_seconds)
    env.resume()
    second = probe(env)
    sleeper.stdin.close()
    sleeper.wait(check=True)
    sleep_monotonic, sleep_realtime = map(float, sleeper.stdout.read().split())
    return {'host': second['host_before'] - first['host_after'],
            'realtime': second['syscall_realtime'] - first['syscall_realtime'],
            'monotonic': second['syscall_monotonic'] - first['syscall_monotonic'],
            'sleep_monotonic': sleep_monotonic, 'sleep_realtime': sleep_realtime}


def test_freeze_pause_stops_guest_time_without_affecting_a_peer():
    pause = 6
    with Sandbox(clock_offset=-DAY, memory=MEMORY) as env, Sandbox(memory=MEMORY) as peer:
        latencies, stop = [], threading.Event()
        def exercise_peer():
            while not stop.is_set():
                started = time.monotonic()
                assert_offset(probe(peer), 0)
                latencies.append(time.monotonic() - started)
        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(exercise_peer)
            try:
                frozen = measure_pause(env, pause)
            finally:
                stop.set()
            future.result()
        assert frozen['host'] >= pause
        # Guest time advanced only by the commands around the pause.
        assert frozen['realtime'] < frozen['host'] - pause + SLACK
        assert frozen['monotonic'] < frozen['host'] - pause + SLACK
        # A three-second sleep spanning the pause still lasted three guest seconds.
        assert 2.9 < frozen['sleep_monotonic'] < 3 + SLACK
        assert 2.9 < frozen['sleep_realtime'] < 3 + SLACK
        # After resume the clock follows the configured offset less the paused time.
        drift = probe(env)['syscall_realtime'] - (time.time() - DAY)
        assert -pause - 3 * SLACK < drift < -pause + SLACK
        assert latencies and max(latencies) < 5
        record('freeze', {**frozen, 'pause_seconds': pause, 'resume_offset_drift': drift,
                          'peer_probes': len(latencies), 'peer_probe_median_seconds': statistics.median(latencies),
                          'peer_probe_max_seconds': max(latencies)})


@pytest.mark.parametrize('options', [{'clock_offset': -DAY, 'clock_pause': 'continue'}, {}])
def test_continue_pause_keeps_guest_time_running(options):
    pause = 4
    with Sandbox(memory=MEMORY, **options) as env:
        running = measure_pause(env, pause)
        assert running['realtime'] >= pause
        assert running['monotonic'] >= pause
        assert running['sleep_monotonic'] >= pause
        assert_offset(probe(env), options.get('clock_offset', 0))
        record('continue-' + ('offset' if options else 'default'), running)


def file_mtime(env, *, write=False):
    if write:
        env.run('echo saved > /root/saved-before-capture', check=True)
    return float(env.run('stat -c %.9Y /root/saved-before-capture', check=True).stdout)


def ticker(env):
    env.run("setsid python3 -u -c 'import time\nwhile True:\n    print(time.time(), flush=True)\n    "
            "time.sleep(.1)' > /tmp/ticks 2>&1 < /dev/null &", check=True)
    deadline = time.monotonic() + 10
    while not env.files.read_text('/tmp/ticks').strip():
        assert time.monotonic() < deadline
        time.sleep(.1)


def latest_tick(env):
    time.sleep(.5)
    before = time.time()
    value = float(env.files.read_text('/tmp/ticks').split()[-1])
    return value - before


def test_memory_checkpoint_restores_with_the_restore_clock():
    with Sandbox(clock_offset=-30 * DAY, memory=MEMORY) as env:
        ticker(env)
        assert abs(latest_tick(env) + 30 * DAY) < SLACK
        written = probe(env)
        saved_mtime = file_mtime(env, write=True)
        checkpoint = env.snapshot(state='memory')
        assert checkpoint.verify()['status'] == 'passed'
    for offset in (5 * DAY, None):
        options = {'clock_offset': offset} if offset is not None else {}
        with Sandbox(snapshot=checkpoint, memory=MEMORY, **options) as restored:
            # The process started before the checkpoint uses the restore clock too.
            assert abs(latest_tick(restored) - (offset or 0)) < SLACK
            reading = probe(restored)
            assert_offset(reading, offset or 0)
            # Monotonic time continued from the checkpoint instead of restarting.
            assert reading['syscall_monotonic'] >= written['syscall_monotonic']
            # Files keep the time at which they were written.
            assert file_mtime(restored) == saved_mtime
            if offset is None:
                assert 'clock' not in restored.info


def test_checkpoint_while_frozen_keeps_time_frozen_until_resume():
    pause = 4
    with Sandbox(clock_offset=-2 * DAY, memory=MEMORY) as env:
        first = probe(env)
        env.pause()
        checkpoint = env.snapshot(state='memory')
        time.sleep(pause)
        env.resume()
        second = probe(env)
        assert second['host_before'] - first['host_after'] >= pause
        assert second['syscall_realtime'] - first['syscall_realtime'] < second['host_before'] - first['host_after'] - pause + SLACK
    with Sandbox(snapshot=checkpoint, clock_offset=-7 * DAY, memory=MEMORY) as restored:
        assert_offset(probe(restored), -7 * DAY)


def test_filesystem_restore_uses_a_new_start_date():
    with Sandbox(clock_offset=-DAY, memory=MEMORY) as env:
        saved_mtime = file_mtime(env, write=True)
        assert abs(saved_mtime - (time.time() - DAY)) < 5
        cache = env.snapshot(state='filesystem')
    start = datetime(2030, 1, 1, tzinfo=timezone.utc)
    created = time.time()
    with Sandbox(snapshot=cache, clock_start=start, memory=MEMORY) as restored:
        reading = probe(restored)
        assert 0 <= reading['syscall_realtime'] - start.timestamp() <= reading['host_after'] - created + SLACK
        # Saved files keep the time at which they were written.
        assert file_mtime(restored) == saved_mtime


def test_pool_members_share_one_offset():
    offset = -123 * DAY
    with Pool(size=3, warm=3, clock_offset=offset, memory=MEMORY) as pool, ExitStack() as stack:
        environments = [stack.enter_context(pool.acquire()) for _ in range(3)]
        assert len({env.id for env in environments}) == 3
        with ThreadPoolExecutor(3) as executor:
            readings = list(executor.map(probe, environments))
    for reading in readings:
        assert_offset(reading, offset)
    record('pool', [reading['syscall_realtime'] - reading['host_before'] for reading in readings])


def test_cpu_throttling_does_not_freeze_guest_time():
    offset = -DAY
    with Sandbox(clock_offset=offset, cpu=CPU(vcpus=1, quota=.25), memory=MEMORY) as env:
        busy = env.exec("python3 -c 'while True: pass'")
        try:
            started = time.time()
            result = env.run("python3 -c 'import time\nm, r, c = time.monotonic(), time.time(), time.process_time()\n"
                             "while time.monotonic() - m < 3: pass\n"
                             "print(time.monotonic() - m, time.time() - r, time.process_time() - c)'",
                             check=True, timeout=60)
            host = time.time() - started
        finally:
            busy.terminate()
        assert_offset(probe(env), offset)
        guest_monotonic, guest_realtime, guest_cpu = map(float, result.stdout.split())
        # The broker throttled the loop, which shared a quarter CPU with the
        # busy process, yet guest time kept the host rate: broker pauses never
        # freeze it. Frozen pauses would stretch this to about 12 seconds.
        assert guest_cpu / guest_monotonic < .5
        assert host < 8
        assert abs(guest_realtime - guest_monotonic) < .1
        record('cpu-throttle', {'host_seconds': host, 'guest_monotonic': guest_monotonic,
                                'guest_realtime': guest_realtime, 'guest_cpu_seconds': guest_cpu})

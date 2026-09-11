"""Public SDK acceptance; explicitly select a disposable worker and disk root."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shlex
import select
import subprocess
import sys
import tempfile
import time

import pytest

from sandweave import Memory, Sandbox

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_DISK_PATH'), reason='explicit disk-memory acceptance directory required')]

PROGRAM = '''import hashlib, mmap, pathlib, time
n = 1408 * 1024**2
a = mmap.mmap(-1, n)
expected = bytes((i % 251 + 1 for i in range(n // 4096)))
a[::4096] = expected
pathlib.Path('/workspace/ready').write_text(hashlib.sha256(expected).hexdigest())
while True:
    p = pathlib.Path('/workspace/verify')
    if p.exists():
        request = p.read_text()
        assert a[::4096] == expected
        pathlib.Path('/workspace/verified').write_text(request)
        p.unlink()
    time.sleep(.05)
'''


@pytest.fixture(scope='module', autouse=True)
def release_test_worker():
    yield
    from sandweave.sandbox.targets import local_connection
    connection = local_connection()
    try:
        connection.call('_shutdown_if_idle')
    finally:
        connection.close()


def wait_file(env, path, expected=None):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        result = env.run('cat ' + shlex.quote(path))
        if result.returncode == 0 and (expected is None or result.stdout == expected):
            return result.stdout
        time.sleep(.2)
    raise TimeoutError(path)


@pytest.mark.skipif(not os.environ.get('SANDWEAVE_DISK_OLD_RUNTIME'),
                    reason='explicit pre-disk-memory engine required')
def test_engine_upgrade_preserves_running_sandbox():
    root = Path(os.environ['SANDWEAVE_DISK_PATH'])
    with Sandbox(memory='256MiB') as existing:
        before = existing.status()['runtime_status']['sentry']
        old_engine = os.readlink(f'/proc/{before["pid"]}/exe')
        assert os.environ['SANDWEAVE_DISK_OLD_RUNTIME'] in old_engine
        existing.files.write_text('/workspace/preserved', 'still running')
        with Sandbox(memory=Memory('256MiB', disk='1792MiB', disk_path=str(root))) as paged:
            new_pid = paged.status()['runtime_status']['sentry']['pid']
            assert os.readlink(f'/proc/{new_pid}/exe') != old_engine
            assert paged.run("python -c 'import mmap; a=mmap.mmap(-1, 1280*1024**2); "
                             "a[::4096]=b\"x\"*(len(a)//4096); "
                             "assert a[::4096] == b\"x\"*(len(a)//4096)'",
                             timeout=180).returncode == 0
            directory = Path(paged.info['disk_memory']['directory'])
        assert not directory.exists()
        assert existing.status()['runtime_status']['sentry'] == before
        assert existing.files.read_text('/workspace/preserved') == 'still running'
        assert existing.run('echo alive').stdout == 'alive\n'


def telemetry(env):
    memory = env.status()['runtime_status']['disk_memory']
    group = Path(memory['cgroup'])
    result = {'id': env.id, 'memory': env.info['memory'], 'disk_memory': env.info['disk_memory']}
    result['cgroup'] = {key: (group / key).read_text() for key in (
        'memory.current', 'memory.peak', 'memory.max', 'memory.events', 'memory.stat',
        'memory.swap.current')}
    events = dict(row.split() for row in result['cgroup']['memory.events'].splitlines())
    assert int(events['oom_kill']) == 0
    assert int(result['cgroup']['memory.swap.current']) == 0
    assert int(result['cgroup']['memory.max']) == 768 * 1024**2
    stats = dict(row.split() for row in result['cgroup']['memory.stat'].splitlines())
    assert int(stats['pgmajfault']) > 0
    return result


def test_paging_concurrency_restore_and_cleanup():
    root = Path(os.environ['SANDWEAVE_DISK_PATH'])
    memory = Memory('256MiB', '512MiB', '1792MiB', str(root))
    environments = []
    directories = []
    receipt = {}
    try:
        with ThreadPoolExecutor(2) as executor:
            futures = [executor.submit(Sandbox, memory=memory, cpu=2) for _ in range(2)]
            # Retain successful creations even if a concurrent creation fails.
            errors = []
            for future in futures:
                try:
                    environments.append(future.result())
                except Exception as error:
                    errors.append(error)
            if errors:
                raise errors[0]
        for env in environments:
            directory = Path(env.info['disk_memory']['directory'])
            directories.append(directory)
            assert directory.parent == root.resolve()
            assert directory.stat().st_mode & 0o777 == 0o700
            assert not list(directory.iterdir())  # backing is an unnamed O_TMPFILE
            assert env.run('test ! -e /disk-memory && test ! -e /dev/kvm').returncode == 0
            assert 'MemTotal:        2097152 kB' in env.run('cat /proc/meminfo').stdout
            env.exec('python3 -u -c ' + shlex.quote(PROGRAM))
        assert directories[0] != directories[1]
        for env in environments:
            wait_file(env, '/workspace/ready')
            env.files.write_text('/workspace/verify', env.id)
            wait_file(env, '/workspace/verified', env.id)
        receipt['concurrent'] = [telemetry(env) for env in environments]
        first = environments[0]
        snapshot = first.snapshot(state='memory')
        assert snapshot.verify()['status'] == 'passed'
        receipt['snapshot'] = snapshot.id
        first.terminate()
        assert not directories[0].exists()
        restored = Sandbox(snapshot=snapshot, memory=Memory('256MiB', '512MiB', '1792MiB', str(root / 'restores')))
        environments.append(restored)
        directories.append(Path(restored.info['disk_memory']['directory']))
        assert directories[-1].parent == root.resolve() / 'restores'
        restored.files.write_text('/workspace/verify', 'restored-data')
        wait_file(restored, '/workspace/verified', 'restored-data')
        receipt['restored'] = telemetry(restored)
        receipt['passed'] = True
    finally:
        for env in reversed(environments):
            env.terminate()
        assert all(not path.exists() for path in directories)
        receipt['directories_released'] = list(map(str, directories))
        if destination := os.environ.get('SANDWEAVE_DISK_RECEIPT'):
            Path(destination).write_text(json.dumps(receipt, indent=2) + '\n')


@pytest.mark.skipif(not os.environ.get('SANDWEAVE_DISK_LARGE'), reason='explicit 20 GiB acceptance requested')
def test_twenty_gibibytes_with_four_gibibytes_ram():
    root = Path(os.environ['SANDWEAVE_DISK_PATH']) / 'large'
    directory = None
    try:
        with Sandbox(memory=Memory('4GiB', '512MiB', '16GiB', str(root)), cpu=2) as env:
            directory = Path(env.info['disk_memory']['directory'])
            assert 'MemTotal:       20971520 kB' in env.run('cat /proc/meminfo').stdout
            command = PROGRAM.split('while True:')[0].replace('1408 * 1024**2', '18 * 1024**3')
            command += '\nassert a[::4096] == expected\nprint("18GiB verified")\n'
            result = env.run('python3 -u -c ' + shlex.quote(command), timeout=300)
            assert result.returncode == 0, result.stderr
            assert result.stdout == '18GiB verified\n'
            memory = env.status()['runtime_status']['disk_memory']
            group = Path(memory['cgroup'])
            receipt = {'memory': env.info['memory'], 'disk_memory': env.info['disk_memory'],
                       'cgroup': {key: (group / key).read_text() for key in
                         ('memory.peak', 'memory.max', 'memory.events', 'memory.stat')}}
            events = dict(row.split() for row in receipt['cgroup']['memory.events'].splitlines())
            assert int(events['oom_kill']) == 0
            assert int(receipt['cgroup']['memory.max']) == 4608 * 1024**2
            if path := os.environ.get('SANDWEAVE_DISK_RECEIPT'):
                Path(path).with_suffix('.large.json').write_text(json.dumps(receipt, indent=2) + '\n')
    finally:
        assert directory is None or not directory.exists()


def test_failed_filesystem_leaves_no_private_directory():
    from sandweave import SandboxError
    with tempfile.TemporaryDirectory(prefix='sandweave-disk-test-', dir='/dev/shm') as directory:
        with pytest.raises(SandboxError, match='disk filesystem'):
            Sandbox(memory=Memory('256MiB', '512MiB', '1GiB', directory), cpu=2)
        assert not list(Path(directory).iterdir())


def test_filesystem_cache_can_restore_without_disk_memory():
    root = Path(os.environ['SANDWEAVE_DISK_PATH'])
    with Sandbox(memory=Memory('256MiB', '512MiB', '1GiB', str(root))) as env:
        env.files.write_text('/workspace/saved-note', 'retained')
        saved = env.snapshot(state='filesystem')
    with Sandbox(snapshot=saved, memory=Memory('512MiB')) as restored:
        assert restored.files.read_text('/workspace/saved-note') == 'retained'
        assert 'disk_memory' not in restored.info
        assert 'disk' not in restored.info['memory']


def test_owner_crash_releases_disk_memory():
    script = '''from sandweave import Sandbox, Memory
import json, os, time
env = Sandbox(memory=Memory('256MiB', '512MiB', '1GiB', os.environ['SANDWEAVE_DISK_PATH']), cpu=2)
print(json.dumps({'id': env.id, 'directory': env.info['disk_memory']['directory']}), flush=True)
time.sleep(3600)
'''
    child = subprocess.Popen([sys.executable, '-u', '-c', script], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([child.stdout], [], [], 180)[0], 'client never became ready'
        line = child.stdout.readline()
        assert line, child.stderr.read()
        record = json.loads(line)
        directory = Path(record['directory'])
        assert directory.is_dir()
        child.kill()
        child.wait(15)
        deadline = time.monotonic() + 60
        while directory.exists() and time.monotonic() < deadline:
            time.sleep(.2)
        assert not directory.exists(), 'crashed client left disk memory behind'
        if destination := os.environ.get('SANDWEAVE_DISK_RECEIPT'):
            Path(destination).with_suffix('.crash.json').write_text(json.dumps({**record, 'released': True}) + '\n')
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(15)
        child.stdout.close()
        child.stderr.close()

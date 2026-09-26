"""Disk-backed files exceed guest RAM and survive independent cold/live restores."""
import concurrent.futures
import json
import os
from pathlib import Path
import time

import pytest
from sandweave import Sandbox, Storage, Memory
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit disposable worker required')]


@pytest.fixture(scope='module', autouse=True)
def stop_owned_worker():
    yield
    c = local_connection()
    try:
        c.call('_shutdown_if_idle')
    finally:
        c.close()


@pytest.mark.parametrize('state', ['filesystem', 'memory'])
def test_disk_larger_than_ram_and_independent_restores(state, tmp_path):
    parent = tmp_path / 'disk-files'
    with Sandbox(storage=Storage(path=str(parent)), memory=Memory('128MiB', '512MiB')) as source:
        directory = Path(source.info['storage']['directory'])
        assert directory.parent == parent
        source.run("python3 -c \"f=open('/workspace/large','wb'); "
                   "b=b'01234567'*131072; [f.write(b) for _ in range(384)]; f.close()\"", timeout=120, check=True)
        source.run("ln /workspace/large /workspace/linked; chmod 640 /workspace/large; "
                   "python3 -c \"import os; os.setxattr('/workspace/large','user.binary',b'\\x00\\xff')\"", check=True)
        expected = source.run('sha256sum /workspace/large', timeout=60, check=True).stdout.split()[0]
        saved = source.snapshot(state=state)
    assert not directory.exists()

    def restore(index):
        with Sandbox(snapshot=saved, storage=Storage(path=str(parent / str(index)))) as env:
            current = Path(env.info['storage']['directory'])
            assert current.parent == parent / str(index)
            assert env.run('sha256sum /workspace/large', timeout=60, check=True).stdout.split()[0] == expected
            env.run("python3 -c \"import os; a=os.stat('/workspace/large'); b=os.stat('/workspace/linked'); "
                    "assert a.st_ino==b.st_ino; assert a.st_mode & 511 == 416; "
                    "assert os.getxattr('/workspace/large','user.binary')==b'\\x00\\xff'\"", check=True)
            env.run('rm /workspace/large /workspace/linked', check=True)
        assert not current.exists()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(restore, range(2)))


def test_explicit_memory_mode_and_cold_conversion(tmp_path):
    with Sandbox(storage='memory') as env:
        assert env.info['storage']['mode'] == 'memory'
        env.files.write_text('/workspace/value', 'preserved')
        saved = env.snapshot(state='filesystem')
    with Sandbox(snapshot=saved, storage=Storage(path=str(tmp_path / 'disk'))) as env:
        assert env.info['storage']['mode'] == 'disk'
        assert env.files.read_text('/workspace/value') == 'preserved'


def test_concurrent_writes_leave_unrelated_commands_responsive(tmp_path):
    result = {'latencies': []}
    with Sandbox(memory=Memory('128MiB', '256MiB')) as control:
        def write(index):
            with Sandbox(memory=Memory('128MiB', '256MiB'), storage=Storage(path=str(tmp_path / 'disk'))) as env:
                return env.run('dd if=/dev/zero of=/workspace/large bs=1M count=384', timeout=120, check=True).returncode
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            writers = [executor.submit(write, i) for i in range(4)]
            while not all(f.done() for f in writers):
                tick = time.monotonic()
                assert control.run('true', timeout=10, check=True).returncode == 0
                result['latencies'].append(time.monotonic() - tick)
                time.sleep(.02)
            assert [f.result() for f in writers] == [0]*4
    result['max_seconds'] = max(result['latencies'])
    (tmp_path / 'concurrent-storage.json').write_text(json.dumps(result, indent=2))
    assert result['max_seconds'] < 5

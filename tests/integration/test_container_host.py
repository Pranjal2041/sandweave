"""Runtime acceptance, also run under scripts/with-seccomp-listener.py."""
from concurrent.futures import ThreadPoolExecutor
import os
import uuid

import pytest

from sandweave import Memory, Sandbox
from sandweave.sandbox.process import Process
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get('SANDWEAVE_INTEGRATION') != '1', reason='requires a prepared runtime')]


@pytest.fixture(scope='module', autouse=True)
def stop_idle_worker():
    yield
    connection = local_connection()
    connection.call('_shutdown_if_idle')
    connection.close()


def test_concurrent_guests_remain_isolated():
    def run(index):
        with Sandbox(memory=Memory(guest='256MiB', runtime='128MiB')) as env:
            marker = uuid.uuid4().hex
            env.files.write_text('/tmp/isolation', marker)
            assert env.run('cat /tmp/isolation').stdout == marker
            assert env.run("python -c 'print(sum(range(1000)))'").stdout == '499500\n'
            assert env.run('test ! -e /dev/kvm').returncode == 0
            return env.id

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert len(set(executor.map(run, range(4)))) == 4


def test_checkpoint_preserves_a_waiting_process():
    with Sandbox(memory=Memory(guest='256MiB', runtime='128MiB')) as env:
        process = env.exec(argv=['python', '-u', '-c',
            'import uuid; value=uuid.uuid4().hex; print("ready"); print(value + input())'])
        assert process.stdout.readline() == 'ready\n'
        snapshot = env.snapshot(state='memory')
        process.stdin.write('source\n')
        source = process.stdout.readline().strip()
        with Sandbox(snapshot=snapshot) as clone:
            restored = Process(clone, process.id)
            assert restored.stdout.readline() == 'ready\n'
            restored.stdin.write('clone\n')
            assert restored.stdout.readline().strip() == source.removesuffix('source') + 'clone'
            restored.stdin.close()
            assert restored.wait(timeout=5) == 0
        process.stdin.close()
        assert process.wait(timeout=5) == 0


def test_network_pause_and_filesystem_restore():
    memory = Memory(guest='256MiB', runtime='128MiB')
    with Sandbox(memory=memory) as env:
        assert env.run('curl -fsS --max-time 20 -o /dev/null -w "%{http_code}" https://example.com',
                       timeout=25).stdout == '200'
        env.files.write_text('/workspace/saved', 'before checkpoint')
        env.pause()
        assert env.status()['state'] == 'paused'
        env.resume()
        checkpoint = env.snapshot(state='filesystem')
        env.files.write_text('/workspace/saved', 'after checkpoint')
        with Sandbox(snapshot=checkpoint) as clone:
            assert clone.files.read_text('/workspace/saved') == 'before checkpoint'
    with Sandbox(memory=memory, network='offline') as env:
        result = env.run("python -c 'import socket; socket.create_connection((\"1.1.1.1\",443),timeout=1)'")
        assert result.returncode != 0

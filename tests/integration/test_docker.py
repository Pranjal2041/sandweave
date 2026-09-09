"""Nested Docker and database/network state through the SDK checkpoint contract."""
import json
import os
from pathlib import Path
import time

import pytest

from sandweave import Sandbox, Memory
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit worker required')]


@pytest.fixture(scope='module', autouse=True)
def release_idle_test_worker():
    yield
    connection = local_connection()
    connection.call('_shutdown_if_idle')
    connection.close()


def wait_http(env, timeout=150):
    deadline = time.monotonic() + timeout
    while True:
        result = env.run('curl --max-time 3 -s -o /dev/null -w "%{http_code}" http://localhost/login/index.php', check=False)
        if result.stdout == '200':
            return
        assert time.monotonic() < deadline, result
        time.sleep(.3)


def test_nested_docker_memory_and_filesystem_restore():
    recipe = {'extends': 'docker', 'runtime_options': {'docker_archive': 'images/gvisor-moodle-persisted-docker.tar'}}
    directory = Path(os.environ['SANDWEAVE_ASSETS']) / 'runs/sdk-acceptance/docker'
    directory.mkdir(parents=True, exist_ok=True)
    with Sandbox(template=recipe, memory=Memory('12GiB', '1GiB')) as env:
        env.run('docker start general-vm-moodle', timeout=60)
        wait_http(env)
        nested = env.run('docker exec general-vm-moodle docker ps --format "{{.Names}} {{.Status}}"').stdout
        assert 'moodle-mariadb Up' in nested
        env.files.upload(Path(os.environ['SANDWEAVE_ASSETS']) / 'scripts/snapshot-probe.py', '/workspace/probe.py')
        env.exec('python -u /workspace/probe.py')
        time.sleep(1)
        command = "python -c 'import urllib.request; print(urllib.request.urlopen(\"http://127.0.0.1:8000\").read().decode())'"
        before = json.loads(env.run(command).stdout)
        saved = env.snapshot(state='memory')
        with Sandbox(snapshot=saved) as restored:
            after = json.loads(restored.run(command).stdout)
            for key in ('nonce', 'pid', 'unlinked_file', 'file_offset', 'marker', 'uid', 'gid', 'mode', 'cross_netns_tcp', 'abstract_queued'):
                assert after[key] == before[key], key
            wait_http(restored)
            assert 'moodle-mariadb Up' in restored.run('docker exec general-vm-moodle docker ps --format "{{.Names}} {{.Status}}"').stdout
            restored.pause(); restored.resume(); wait_http(restored)
            (directory / 'memory.json').write_text(json.dumps({'before': before, 'restored': after, 'nested': nested}, indent=2))
        cold = env.stop(state='filesystem')
    with Sandbox(cache=cold) as restored:
        restored.run('docker start general-vm-moodle', timeout=60)
        wait_http(restored)
        assert 'moodle-mariadb Up' in restored.run('docker exec general-vm-moodle docker ps --format "{{.Names}} {{.Status}}"').stdout
        (directory / 'filesystem.json').write_text(json.dumps({'snapshot': cold.id, 'moodle_http': 200, 'nested_database': 'running'}))

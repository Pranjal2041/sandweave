"""Inspect real disposable desktops, command sandboxes and selected GPUs."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from sandweave import Sandbox

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit worker required')]


@pytest.fixture(scope='module')
def workers():
    connections = {}
    yield connections
    for connection in connections.values():
        try:
            connection.call('_shutdown_if_idle')
        finally:
            connection.close()


def remember(env, workers):
    connection = env._connection
    workers[connection.port, connection.token] = connection


def save(name, info):
    if directory := os.environ.get('SANDWEAVE_TEST_ARTIFACTS'):
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        (path / (name + '.json')).write_text(json.dumps(info, indent=2))


def rfb_banner(port):
    with socket.create_connection(('127.0.0.1', port), timeout=5) as connection:
        data = b''
        while len(data) < 12:
            chunk = connection.recv(12 - len(data))
            assert chunk, 'VNC closed before its protocol banner'
            data += chunk
        assert data.startswith(b'RFB 003.') and data.endswith(b'\n'), data


def test_gnome_info_cli_and_vnc_follow_the_live_lifecycle(workers):
    with Sandbox(template='gnome') as env:
        remember(env, workers)
        info = env.info
        assert info['worker']['hostname'] == socket.gethostname()
        assert info['worker']['job_id'] == os.environ.get('SLURM_JOB_ID')
        assert info['cpu']['vcpus'] == 4
        assert info['memory'] == {'guest': '8GiB', 'runtime': '1GiB'}
        assert info['gpus'] == []
        assert int(env.run("python -c 'import os; print(os.cpu_count())'").stdout) == info['cpu']['vcpus']
        memory = env.run('cat /proc/meminfo').stdout
        total_kib = next(int(line.split()[1]) for line in memory.splitlines() if line.startswith('MemTotal:'))
        assert total_kib * 1024 == 8 * 1024**3
        rfb_banner(info['vnc']['port'])
        cli = subprocess.run([sys.executable, '-m', 'sandweave.cli', 'info', env.id],
                             capture_output=True, text=True, check=True, timeout=30)
        assert json.loads(cli.stdout) == info
        save('gnome-ready', info)
        env.pause()
        assert env.info['state'] == 'paused' and env.info['vnc'] is None
        env.resume()
        assert env.info['vnc'] == info['vnc']
        rfb_banner(env.info['vnc']['port'])
        env.terminate()
        final = env.info
        assert final['state'] == 'terminated' and final['vnc'] is None and final['gpus'] == []
        save('gnome-terminated', final)


@pytest.mark.parametrize('runtime', ['gvisor', 'apptainer'])
def test_command_sandbox_reports_resources_without_a_spurious_vnc_url(runtime, workers):
    with Sandbox(runtime=runtime, cpu=2, memory='2GiB') as env:
        remember(env, workers)
        info = env.info
        assert info['runtime'] == runtime
        assert info['cpu']['vcpus'] == 2 and info['memory']['guest'] == '2GiB'
        assert info['gpus'] == [] and info['vnc'] is None
        expression = 'len(os.sched_getaffinity(0))' if runtime == 'apptainer' else 'os.cpu_count()'
        assert int(env.run("python -c 'import os; print(" + expression + ")'").stdout) == 2
        save(runtime + '-coding', info)


@pytest.mark.gpu
@pytest.mark.skipif(not os.environ.get('SANDWEAVE_GPU_INTEGRATION'), reason='explicit allocated GPU required')
@pytest.mark.parametrize('runtime', ['gvisor', 'apptainer'])
def test_gpu_information_matches_the_device_visible_inside_the_guest(runtime, workers):
    with Sandbox(runtime=runtime, gpu=True) as env:
        remember(env, workers)
        info = env.info
        assert len(info['gpus']) == 1
        selected = info['gpus'][0]
        assert selected['model'] and selected['uuid'] and selected['device'].startswith('/dev/nvidia')
        devices = env.run('nvidia-smi --query-gpu=uuid,name --format=csv,noheader').stdout.strip().splitlines()
        assert len(devices) == 1
        uuid, model = (part.strip() for part in devices[0].split(',', 1))
        assert (selected['uuid'], selected['model']) == (uuid, model)
        env.pause()
        assert env.info['gpus'] == info['gpus']
        env.resume()
        save(runtime + '-gpu', info)
        env.terminate()
        assert env.info['gpus'] == []

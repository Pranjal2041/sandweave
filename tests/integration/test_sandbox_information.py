"""Inspect real disposable desktops, command sandboxes and selected GPUs."""
import json
import os
from pathlib import Path
import socket
import struct
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


def rfb_authenticate(vnc):
    """Complete VNC authentication and read the server's actual framebuffer size."""
    def receive(connection, size):
        data = b''
        while len(data) < size:
            chunk = connection.recv(size - len(data))
            assert chunk, 'VNC closed during its handshake'
            data += chunk
        return data

    with socket.create_connection(('127.0.0.1', vnc['port']), timeout=5) as connection:
        assert receive(connection, 12) == b'RFB 003.008\n'
        connection.sendall(b'RFB 003.008\n')
        choices = receive(connection, receive(connection, 1)[0])
        assert 2 in choices, 'VNC password authentication is not offered'
        connection.sendall(b'\x02')
        challenge = receive(connection, 16)
        password = vnc['password'].encode()[:8].ljust(8, b'\0')
        key = bytes(int(f'{byte:08b}'[::-1], 2) for byte in password)
        response = subprocess.run(['openssl', 'enc', '-des-ecb', '-nopad', '-nosalt',
                                   '-provider', 'default', '-provider', 'legacy', '-K', key.hex()],
                                  input=challenge, capture_output=True, check=True, timeout=5).stdout
        connection.sendall(response)
        assert receive(connection, 4) == b'\0\0\0\0', 'advertised VNC password was rejected'
        connection.sendall(b'\x01')  # Share the existing desktop.
        width, height = struct.unpack('!HH', receive(connection, 4))
        assert (width, height) == (1920, 1080)


def test_gnome_info_cli_and_vnc_follow_the_live_lifecycle(workers):
    with Sandbox(template='gnome') as env:
        remember(env, workers)
        info = env.info
        assert info['worker'] == {'hostname': socket.gethostname()}
        assert set(info['vnc']) == {'url', 'port', 'worker_host', 'password'}
        assert info['cpu']['vcpus'] == 4
        assert info['memory'] == {'guest': '8GiB', 'runtime': '1GiB'}
        assert info['gpus'] == []
        assert int(env.run("python -c 'import os; print(os.cpu_count())'").stdout) == info['cpu']['vcpus']
        memory = env.run('cat /proc/meminfo').stdout
        total_kib = next(int(line.split()[1]) for line in memory.splitlines() if line.startswith('MemTotal:'))
        assert total_kib * 1024 == 8 * 1024**3
        rfb_authenticate(info['vnc'])
        image = env.desktop.screenshot()
        assert image.size == (1920, 1080)
        if directory := os.environ.get('SANDWEAVE_TEST_ARTIFACTS'):
            Path(directory).mkdir(parents=True, exist_ok=True)
            image.save(Path(directory) / 'gnome-1080p.png')
        cli = subprocess.run([sys.executable, '-m', 'sandweave.cli', 'info', env.id],
                             capture_output=True, text=True, check=True, timeout=30)
        assert json.loads(cli.stdout) == info
        save('gnome-ready', info)
        env.pause()
        assert env.info['state'] == 'paused' and env.info['vnc'] is None
        env.resume()
        assert env.info['vnc'] == info['vnc']
        rfb_authenticate(env.info['vnc'])

        # Fresh installations retain generated plaintext; legacy images do not.
        env.run("printf 'fresh123longer\n' > /etc/sandweave-vnc-password; "
                "tigervncpasswd -f < /etc/sandweave-vnc-password > /home/ga/.vnc/passwd", user='root')
        generated = env.info['vnc']
        assert generated['password'] == 'fresh123'
        rfb_authenticate(generated)
        env.run("printf 'stale123\n' > /etc/sandweave-vnc-password", user='root')
        assert env.info['vnc']['password'] is None
        env.run('rm /etc/sandweave-vnc-password', user='root')
        assert env.info['vnc']['password'] is None
        env.run("printf 'labvnc01\n' | tigervncpasswd -f > /home/ga/.vnc/passwd", user='root')
        legacy = env.info['vnc']
        assert legacy['password'] == 'labvnc01'
        rfb_authenticate(legacy)
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

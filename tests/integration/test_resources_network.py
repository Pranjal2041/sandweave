"""Guest-visible resources, real egress isolation and sampled CPU enforcement."""
import json
import os
from pathlib import Path
import socket
import subprocess
import time

import pytest

from sandweave import Sandbox, CPU, Memory
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit worker required')]


@pytest.fixture(scope='module', autouse=True)
def release_idle_test_worker():
    yield
    connection = local_connection()
    connection.call('_shutdown_if_idle')
    connection.close()


def test_cpu_memory_views_and_guest_allocation_limit():
    with Sandbox(cpu=3, memory=Memory('192MiB', '512MiB')) as env:
        info = json.loads(env.run("python -c 'import os,json; print(json.dumps(dict(cpus=os.cpu_count(),memory=open(\"/proc/meminfo\").read())))'").stdout)
        assert info['cpus'] == 3
        total = int(info['memory'].splitlines()[0].split()[1]) * 1024
        assert total == 192 * 1024**2
        allocation = env.run("python -c 'x=bytearray(512*1024**2); print(len(x))'", timeout=15, check=False)
        assert allocation.returncode != 0, 'guest exceeded its configured page budget'
        assert env.run('echo alive').stdout == 'alive\n'


@pytest.mark.parametrize('network', ['internet', 'offline'])
def test_host_canary_isolation_and_selected_egress(network):
    with socket.socket() as canary, Sandbox(network=network) as env:
        canary.bind(('0.0.0.0', 0)); canary.listen()
        port = canary.getsockname()[1]
        interfaces = json.loads(subprocess.check_output(['ip', '-j', 'address']))
        hosts = ['10.0.2.2', *[a['local'] for i in interfaces for a in i['addr_info']
                              if a['family'] == 'inet' and not a['local'].startswith('127.')]]
        code = 'import socket,json\nr={}\nfor host in ' + repr(hosts) + ':\n' + \
               ' try:\n  s=socket.create_connection((host,' + str(port) + '),timeout=.4);s.close();r[host]=True\n' + \
               ' except OSError:r[host]=False\nprint(json.dumps(r))'
        assert not any(json.loads(env.run(argv=['python', '-c', code]).stdout).values())
        canary.settimeout(.1)
        with pytest.raises(TimeoutError):
            canary.accept()
        if network == 'internet':
            assert env.run('curl -fsS --max-time 15 -o /dev/null -w "%{http_code}" https://example.com', timeout=20).stdout == '200'
        else:
            code = '''import socket
try:
 s=socket.create_connection(('1.1.1.1',443),timeout=1)
except OSError:pass
else:raise AssertionError('offline TCP connected')
with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:
 s.settimeout(1)
 s.sendto(bytes.fromhex('123401000001000000000000076578616d706c6503636f6d0000010001'),('10.0.2.3',53))
 try:s.recv(1024)
 except TimeoutError:pass
 else:raise AssertionError('offline DNS replied')
'''
            env.run(argv=['python', '-c', code], timeout=5)


def test_sampled_quota_accounts_runtime_cpu():
    with Sandbox(cpu=CPU(vcpus=4, quota=1), memory='512MiB') as env:
        root = Path(env.status()['workspace'])
        local = Path((root / 'runs/local-path.txt').read_text().strip())
        def usage():
            for path in (local / 'gvisor/cpu-brokers').glob('*/status.json'):
                data = json.loads(path.read_text())
                key = 'job-' + env.id + '.json'
                if key in data['jobs']:
                    return data['jobs'][key]['cpu_seconds']
            raise AssertionError('sandbox was not registered with CPU broker')
        process = env.exec("python -u -c 'import os; [os.fork() for _ in range(2)]; exec(\"while True: pass\")'")
        time.sleep(1)
        before = usage(); started = time.monotonic()
        time.sleep(8)
        rate = (usage() - before) / (time.monotonic() - started)
        process.terminate()
        assert .55 < rate < 1.5, ('sampled one-CPU quota', rate)
        directory = Path(os.environ['SANDWEAVE_ASSETS']) / 'runs/sdk-acceptance/resources'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'quota.json').write_text(json.dumps({'cpu_equivalents': rate, 'target': 1}))


def test_weighted_sharing_and_borrowing_idle_capacity():
    # Read only registrations belonging to these two SDK-owned environments.
    with Sandbox(cpu=CPU(vcpus=8, weight=300), memory='512MiB') as first, \
         Sandbox(cpu=CPU(vcpus=8, weight=100), memory='512MiB') as second:
        root = Path(first.status()['workspace'])
        local = Path((root / 'runs/local-path.txt').read_text().strip())
        def usage():
            for path in (local / 'gvisor/cpu-brokers').glob('*/status.json'):
                values = json.loads(path.read_text())['jobs']
                keys = ['job-' + env.id + '.json' for env in (first, second)]
                if all(key in values for key in keys):
                    return [values[key]['cpu_seconds'] for key in keys]
            raise AssertionError('both peers must share the same CPU broker')
        command = "python -c 'import os; [os.fork() for _ in range(3)]; exec(\"while True: pass\")'"
        a, b = first.exec(command), second.exec(command)
        time.sleep(1)
        before = usage(); time.sleep(8); after = usage()
        used = [end-start for start,end in zip(before, after)]
        ratio = used[0]/used[1]
        assert 1.8 < ratio < 4.5, (used, ratio)
        a.terminate()
        before = usage(); time.sleep(4); after = usage()
        borrowed_rate = (after[1]-before[1])/4
        b.terminate()
        assert borrowed_rate > used[1]/8 * 1.5
        directory = Path(os.environ['SANDWEAVE_ASSETS']) / 'runs/sdk-acceptance/resources'
        (directory / 'sharing.json').write_text(json.dumps({'ratio': ratio, 'cpu_seconds': used,
                                                          'borrowed_cpu_equivalents': borrowed_rate}))


def test_broker_failure_releases_only_its_owned_guest():
    import sys
    with Sandbox(cpu=CPU(vcpus=2, quota=.2)) as env:
        # Wait for the broker's quota pause, which differs from an SDK pause
        # of guest tasks. Kill only this dedicated broker after that fence.
        env.exec("python -c 'exec(\"while True: pass\")'")
        root = Path(env.status()['workspace'])
        result = subprocess.run([sys.executable,
            str(Path(os.environ['SANDWEAVE_ASSETS']) / 'scripts/test-cpu-controller-failure.py'),
            env.id, '--lab', str(root)], capture_output=True, text=True, timeout=45)
        assert result.returncode == 0, result.stdout + result.stderr
        assert env.status()['runtime_status']['status'] == 'stopped'

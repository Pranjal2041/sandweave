"""Bridge switching, routing and snapshots with real guest sockets.

Uses a disposable worker selected by SANDWEAVE_HOME/SANDWEAVE_ASSETS. The
20,000-connection test fails against runtime 2026.09.25.1 with duplicated,
port-remapped SYNs even after all firewall rules are flushed.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import time

import pytest

from sandweave import Sandbox
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit disposable worker required')]
PROBE = Path(__file__).with_name('probe-bridge-network.py')
if not PROBE.is_file():
    PROBE = Path(__file__).resolve().parents[2] / 'scripts/probe-bridge-network.py'


@pytest.fixture(scope='module', autouse=True)
def stop_idle_worker():
    yield
    connection = local_connection()
    try:
        connection.call('_shutdown_if_idle')
    finally:
        connection.close()


@pytest.mark.parametrize('concurrency', [1, 16])
def test_switched_unicast_never_enters_ip_forwarding(concurrency, tmp_path):
    with Sandbox(template='docker', memory='3GiB') as env, Sandbox(memory='512MiB') as other:
        env.files.upload(PROBE, '/tmp/probe.py')
        env.run('iptables -F; iptables -P FORWARD ACCEPT; iptables -t nat -F', check=True)
        def probe():
            return env.run(f'python /tmp/probe.py probe --count 20000 --concurrency {concurrency}',
                           timeout=180, check=True)
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(probe)
            latencies = []
            while not pending.done():
                started = time.monotonic()
                assert other.run('echo unrelated', timeout=10).stdout == 'unrelated\n'
                latencies.append(time.monotonic()-started)
                time.sleep(.1)
            pending.result()
        result = json.loads(env.files.read_text('/tmp/bridge-probe/result.json'))
        (tmp_path/'result.json').write_text(json.dumps(result, indent=2))
        (tmp_path/'unrelated.json').write_text(json.dumps(latencies))
        assert result['forwarding'] == '1'
        assert result['errors'] == []
        capture = result['capture']
        assert capture['accepted'] == 20000
        assert capture['ttl'] == {'64': 20000}, capture
        assert len(capture['source_macs']) == 1
        assert set(capture['syns']) == {str(i) for i in range(30000, 50000)}


def test_docker_bridge_nat_gateway_and_memory_restore(tmp_path):
    with Sandbox(template='docker', memory='4GiB') as env:
        env.files.upload(PROBE, '/tmp/probe.py')
        env.run('docker pull python:3.12-slim', timeout=180, check=True)
        env.run('docker network create --subnet 10.83.0.0/24 probe', check=True)
        env.run('docker run -d --name server --network probe --ip 10.83.0.3 '
                '-p 18080:8080 -v /tmp/probe.py:/probe.py:ro python:3.12-slim '
                'python /probe.py server --report /tmp/capture.json', timeout=60, check=True)
        env.run('docker run -d --name client --network probe --ip 10.83.0.2 '
                '-v /tmp/probe.py:/probe.py:ro python:3.12-slim sleep infinity', timeout=60, check=True)
        deadline = time.monotonic()+15
        while env.run('docker exec server test -e /tmp/capture.json.ready').returncode:
            assert time.monotonic() < deadline
            time.sleep(.1)
        # Snapshot the actual Docker-created bridge and its learned FDB before
        # the load. Both the original and restored graph must remain usable.
        env.run('docker exec client python /probe.py client --count 1 --start-port 25000', check=True)
        saved = env.snapshot(state='memory')
        with Sandbox(snapshot=saved) as restored:
            for label, guest in [('original', env), ('restored', restored)]:
                result = json.loads(guest.run('docker exec client python /probe.py client '
                                    '--count 20000 --concurrency 16', timeout=180, check=True).stdout)
                assert result['errors'] == [], result
                assert guest.run('cat /proc/sys/net/ipv4/ip_forward', check=True).stdout.strip() == '1'
                # Published DNAT, bridge gateway and unchanged outbound HTTPS.
                code = "import socket; s=socket.create_connection(('127.0.0.1',18080),3); s.sendall(b'published'); assert s.recv(100)==b'published'"
                guest.run(argv=['python', '-c', code], check=True)
                code = "import socket; s=socket.create_connection(('10.83.0.1',18080),3); s.sendall(b'gateway'); assert s.recv(100)==b'gateway'"
                guest.run(argv=['docker', 'exec', 'client', 'python', '-c', code], check=True)
                # Cross-bridge access to a published port traverses real
                # DNAT/forwarding, unlike Docker's same-bridge userland proxy.
                guest.run('docker network create --subnet 10.84.0.0/24 routed', check=True)
                guest.run(argv=['docker', 'run', '--rm', '--network', 'routed',
                                'python:3.12-slim', 'python', '-c', code], timeout=30, check=True)
                guest.run('docker exec client python -c "import urllib.request; '
                          'assert urllib.request.urlopen(\'https://example.com\', timeout=15).status==200"',
                          timeout=20, check=True)
                guest.run('docker exec server touch /tmp/capture.json.stop', check=True)
                guest.run('docker wait server', timeout=10, check=True)
                guest.run('docker cp server:/tmp/capture.json /tmp/capture.json', check=True)
                capture = json.loads(guest.files.read_text('/tmp/capture.json'))
                (tmp_path/(label+'.json')).write_text(json.dumps({'load': result, 'capture': capture}, indent=2))
                # Only the deliberate cross-bridge DNAT connection was routed.
                assert capture['accepted'] == 20004, capture
                assert capture['ttl'] == {'64': 20003, '63': 1}, capture


def test_ipv6_bridge_neighbor_discovery_and_tcp():
    # Docker --ipv6 needs additional per-interface IPv6 sysctls that predate
    # this fix. Test the bridge's IPv6 data path directly with namespaces.
    with Sandbox(memory='1GiB') as env:
        env.files.upload(PROBE, '/tmp/probe.py')
        env.run('python /tmp/probe.py probe --count 1', check=True)
        env.run('ip -n a -6 addr add fd83::2/64 dev eth0; '
                'ip -n b -6 addr add fd83::3/64 dev eth0', check=True)
        server = env.exec('ip netns exec b python -u -c "import socket; '
                          's=socket.socket(socket.AF_INET6); s.bind((\'::\',8081)); '
                          's.listen(); print(\'READY\',flush=True); '
                          'c,a=s.accept(); c.sendall(b\'ipv6\')"')
        assert server.stdout.readline().strip() == 'READY'
        env.run('ip netns exec a python -c "import socket; '
                's=socket.create_connection((\'fd83::3\',8081),3); assert s.recv(100)==b\'ipv6\'"', check=True)
        server.wait(timeout=10, check=True)

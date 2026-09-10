"""A controller on a second host and an outbound worker with enforced limits.

Requires an existing SSH-accessible host with this checkout/interpreter on shared
storage. No allocation is acquired, and only the specified disposable directories
and worker processes belong to this fixture.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

from sandweave import Cluster, Sandbox
from sandweave.sandbox.connection import Connection
from sandweave.sandbox.ownership import process_alive
from sandweave.sandbox.targets import _ssh
from sandweave.weave.worker import start

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_NETWORK_INTEGRATION') or not os.environ.get('SANDWEAVE_CONTROLLER_HOST'),
    reason='explicit disposable storage and remote controller host required')]


def wait_for(function, timeout=240):
    deadline = time.monotonic() + timeout
    while not function():
        if time.monotonic() > deadline:
            raise TimeoutError('network acceptance condition timed out')
        time.sleep(.1)


def test_remote_controller_https_ssh_and_capped_outbound_worker(monkeypatch):
    root = Path(os.environ['SANDWEAVE_NETWORK_INTEGRATION']).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    host = os.environ['SANDWEAVE_CONTROLLER_HOST']
    certificate, key = root / 'server.crt', root / 'server.key'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
        '-subj', '/CN=' + host, '-addext', 'subjectAltName=DNS:' + host,
        '-keyout', str(key), '-out', str(certificate)], check=True, capture_output=True)
    import sandweave
    package = str(Path(sandweave.__file__).resolve().parent.parent)
    remote_environment = ['env', 'SANDWEAVE_HOME=' + str(root / 'controller-client'), 'PYTHONPATH=' + package]
    program = ('from sandweave import Cluster; import json,sys; '
        'c=Cluster.start("weave-network", directory=sys.argv[1], local_worker=False, '
        'listen="0.0.0.0:0", tls_cert=sys.argv[2], tls_key=sys.argv[3]); '
        'print(json.dumps(c.info)); c.close()')
    result = json.loads(_ssh(host, [*remote_environment, sys.executable, '-c', program,
        str(root / 'controller'), str(certificate), str(key)]))
    url = result['connection']['address']
    token_file = root / 'controller/credentials.json'
    assert host in url and host != socket.gethostname()
    monkeypatch.setenv('SANDWEAVE_HOME', str(root / 'worker-data'))
    monkeypatch.setenv('SANDWEAVE_ASSETS', str(Path.cwd()))
    cluster = Cluster.connect(url, token_file=token_file, ca_file=certificate)
    agent = None
    try:
        agent = json.loads(subprocess.check_output([sys.executable, '-m', 'sandweave.cli', 'cluster', 'join', url,
            '--token-file', str(token_file), '--ca-file', str(certificate),
            '--cpus', '2', '--gpus', '0', '--memory', '4GiB', '--slots', '2'], text=True))
        wait_for(lambda: any(w['state'] == 'ready' for w in cluster.workers))
        worker = next(w for w in cluster.workers if w['state'] == 'ready')
        assert worker['capacity']['cpus'] == 2
        assert worker['capacity']['gpu'] == 0
        assert worker['capacity']['memory'] == 4*1024**3
        assert worker['cpu_ids'] == sorted(os.sched_getaffinity(0))[:2]
        # A repeated join must retain the existing agent and its reservations.
        assert start(cluster.config, cpus=2, gpus=0, memory='4GiB', slots=2)['pid'] == agent['pid']
        from sandweave.weave import providers
        original = providers.direct
        def no_worker_route(endpoint, **kwargs):
            if endpoint['hostname'] == socket.gethostname():
                raise AssertionError('client tried to bypass the controller')
            return original(endpoint, **kwargs)
        monkeypatch.setattr(providers, 'direct', no_worker_route)
        with Sandbox(target=cluster) as env:
            env.files.write_text('/workspace/network', 'through the controller')
            assert env.run('cat /workspace/network').stdout == 'through the controller'
            assert env.run('test ! -e /dev/kvm').returncode == 0
            assert env.info['worker']['hostname'] == socket.gethostname()
        # The real SSH tunnel reaches the TLS controller's private RPC port.
        with Cluster.connect('ssh://' + host + str(root / 'controller')) as ssh_cluster:
            assert ssh_cluster.info['id'] == cluster.info['id']
            with Sandbox(target=ssh_cluster) as env:
                assert env.run('printf ssh-forwarded').stdout == 'ssh-forwarded'
        # Run a client on the remote host too: its only configured address is
        # the controller, while the actual sandbox remains on this worker.
        client_program = ('from sandweave import Cluster,Sandbox; import sys,json; '
            'c=Cluster.connect(sys.argv[1],token_file=sys.argv[2],ca_file=sys.argv[3]); '
            'e=Sandbox(target=c); r=e.run("printf remote-client"); '
            'print(json.dumps([r.stdout,e.info["worker"]["hostname"]])); e.terminate(); e.close(); c.close()')
        remote_result = json.loads(_ssh(host, [*remote_environment, sys.executable, '-c', client_program,
            url, str(token_file), str(certificate)]))
        assert remote_result == ['remote-client', socket.gethostname()]
        wait_for(lambda: all(a['released'] for a in cluster.info['sandboxes']))
        cluster.drain(worker['id'])
        cluster.remove_worker(worker['id'])
        wait_for(lambda: process_alive(agent['process']) is False)
    finally:
        try:
            for allocation in cluster.info['sandboxes']:
                cluster.connection.call('allocation_cancel', identity=allocation['id'])
            wait_for(lambda: all(a['released'] for a in cluster.info['sandboxes']))
            for worker in cluster.workers:
                cluster.remove_worker(worker['id'])
            if agent:
                wait_for(lambda: process_alive(agent['process']) is False)
            cluster.stop()
        finally:
            cluster.close()
            for marker in (root / 'worker-data/connections').glob('*/worker.json'):
                info = json.loads(marker.read_text())
                worker_connection = Connection('127.0.0.1', info['port'], info['token'])
                try:
                    worker_connection.call('_shutdown_if_idle')
                finally:
                    worker_connection.close()

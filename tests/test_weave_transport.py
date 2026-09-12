"""Real RPC sockets with no client-to-worker or controller-to-worker connection."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import pytest

from sandweave import Cluster, Sandbox
from sandweave.sandbox.connection import Connection
from sandweave.sandbox.errors import OperationUnknown, SandboxError
from sandweave.sandbox.wire import encode, decode
from sandweave.weave import providers
from sandweave.weave.relay import Broker, RelayConnection
from sandweave.weave.transport import address, credential
from sandweave.weave.worker import Bridge, limits
from test_weave import Executor


@contextmanager
def executor_server(executor):
    executor.open_connections = 0
    connection_guard = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def setup(self):
            super().setup()
            with connection_guard:
                executor.open_connections += 1
        def finish(self):
            try:
                super().finish()
            finally:
                with connection_guard:
                    executor.open_connections -= 1
        def log_message(self, *args):
            pass
        def do_POST(self):
            request = decode(self.rfile.read(int(self.headers['Content-Length'])))
            token = self.headers.get('X-Sandweave-Token')
            try:
                if token != 'private-worker-token' and not executor.management.authorize(token, request['op'], request['params']):
                    raise PermissionError('wrong sandbox credential')
                result = {'result': executor.call(request['op'], **request['params'])}
            except Exception as error:
                result = {'error': {'kind': type(error).__name__, 'message': str(error)}}
            payload = encode(result)
            self.send_response(200)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    class Server(ThreadingHTTPServer):
        # Match the worker listener: the default backlog of five is smaller
        # than the simultaneous clients in the connection reuse test.
        request_queue_size = socket.SOMAXCONN
    server = Server(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    executor.index = server.server_port
    try:
        yield server.server_port
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_http_client_and_outbound_worker_need_only_the_controller(tmp_path, monkeypatch):
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'client'))
    cluster = Cluster.start('http', directory=tmp_path / 'controller', local_worker=False, listen='127.0.0.1:0')
    token_file = tmp_path / 'controller/credentials.json'
    url = cluster.info['connection']['address']
    remote = Cluster.connect(url, token_file=token_file)
    bridge = None
    executor = Executor(tmp_path / 'executor', 1)
    with executor_server(executor) as port:
        channel = 'a'*32
        bridge = Bridge(remote.config, channel, connections=1).start()
        try:
            # The separate controller process also uses only this outbound
            # channel: it has no direct target in its worker registration.
            worker = remote.add_worker({'endpoint': dict(hostname=socket.gethostname(), port=port,
                token='private-worker-token', relay=channel)}, slots=2)
            assert worker['capacity']['slots'] == 2
            monkeypatch.setattr(providers, 'direct', lambda *a, **k: pytest.fail('client tried a direct worker connection'))
            with Sandbox(target=remote, detached=True) as first, Sandbox(target=remote, detached=True) as second:
                assert first._call('describe')['id'] == first.id
                assert second._call('describe')['id'] == second.id
                # Forwarding must preserve worker-side sandbox authorization.
                with pytest.raises(PermissionError):
                    first._connection._worker(first._connection._route(first.id)).call('describe', identity=second.id)
            for _ in range(8):
                with Sandbox(target=remote, detached=True) as env:
                    assert env._call('describe')['id'] == env.id
            deadline = time.monotonic() + 5
            while any(not a['released'] for a in remote.info['sandboxes']):
                assert time.monotonic() < deadline
                time.sleep(.02)
            while executor.open_connections > 4:
                assert time.monotonic() < deadline
                time.sleep(.02)
            assert executor.starts == 10
            remote.stop()
        finally:
            bridge.close()
            remote.close()
            cluster.close()


def test_relay_timeout_never_redelivers_an_uncertain_command():
    broker = Broker()
    endpoint = dict(relay='b'*32, token='test')
    with ThreadPoolExecutor() as threads:
        future = threads.submit(RelayConnection(broker, endpoint, timeout=.1).call, 'mutate')
        request = broker.poll(endpoint['relay'])
        with pytest.raises(OperationUnknown):
            future.result()
        broker.result(endpoint['relay'], request['id'], {'result': 'late'})
        assert not broker.pending
        assert not broker.queues
        assert broker.bytes == 0
    broker.close()


def test_relay_correlates_binary_results_and_duplicate_acknowledgements():
    broker = Broker()
    endpoint = dict(relay='c'*32, token='test')
    with ThreadPoolExecutor() as threads:
        futures = [threads.submit(RelayConnection(broker, endpoint).call, 'binary', data=bytes([i])*1000) for i in range(4)]
        requests = [broker.poll(endpoint['relay']) for _ in range(4)]
        for request in reversed(requests):
            response = {'result': request['parameters']['data']}
            broker.result(endpoint['relay'], request['id'], response)
            broker.result(endpoint['relay'], request['id'], response)
        assert [f.result() for f in futures] == [bytes([i])*1000 for i in range(4)]
    broker.close()


def test_tls_authentication_and_certificate_verification(tmp_path, monkeypatch):
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'client'))
    cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
        '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1',
        '-keyout', str(key), '-out', str(cert)], check=True, capture_output=True)
    cluster = Cluster.start('tls', directory=tmp_path / 'controller', local_worker=False,
        listen='127.0.0.1:0', tls_cert=cert, tls_key=key)
    url = cluster.info['connection']['address']
    token_file = tmp_path / 'controller/credentials.json'
    try:
        # A TCP peer that never begins TLS cannot block other clients.
        idle = socket.create_connection(('127.0.0.1', int(url.rsplit(':', 1)[1])))
        from sandweave.weave.transport import join_link
        link = join_link(url, credential({'token_file': token_file}))
        with Cluster.connect(link, ca_file=cert) as client:
            assert client.info['id'] == cluster.info['id']
        idle.close()
        with Cluster.connect(url, token_file=token_file) as client:
            with pytest.raises(OperationUnknown, match='CERTIFICATE_VERIFY_FAILED'):
                client.info
        with Cluster.connect(url, token='incorrect', ca_file=cert) as client:
            with pytest.raises(SandboxError, match='403'):
                client.info
        with Cluster.connect(link + 'wrong', token_file=token_file, ca_file=cert) as client:
            assert client.info['id'] == cluster.info['id']  # Explicit credential wins.
        # Stop/restart retains the listener port, token and TLS settings.
        cluster.stop()
        from sandweave.sandbox.ownership import process_alive
        info = json.loads((tmp_path / 'controller/controller.json').read_text())
        deadline = time.monotonic() + 5
        while process_alive(info['process']) is not False:
            assert time.monotonic() < deadline
            time.sleep(.02)
        replacement = Cluster.start('tls', directory=tmp_path / 'controller', local_worker=False)
        with Cluster.connect(url, token_file=token_file, ca_file=cert) as client:
            assert client.info['id'] == replacement.info['id']
        replacement.stop(); replacement.close()
    finally:
        cluster.close()


def test_explicit_addresses_and_credentials(tmp_path, monkeypatch):
    token_file = tmp_path / 'token'
    token_file.write_text('file-token\n')
    monkeypatch.setenv('SANDWEAVE_TOKEN', 'environment-token')
    assert credential({'token_file': str(token_file)}) == 'file-token'
    assert credential({'token': 'explicit'}) == 'explicit'
    assert credential({}) == 'environment-token'
    assert address('https://master.example:8443/weave') == {'url': 'https://master.example:8443/weave'}
    ssh = address('ssh://alice@master.example:2222/data/controller')
    assert (ssh['ssh_host'], ssh['ssh_port'], ssh['directory']) == ('alice@master.example', 2222, '/data/controller')
    from sandweave.weave.client import cluster_config
    assert cluster_config('ssh://alice@master.example:2222/data/controller') == ssh
    assert cluster_config('ssh://worker-host') is None  # Existing direct worker syntax.
    for url in ('https://user:secret@host', 'http://host?token=secret', 'ssh://host', 'grpc://host:99'):
        with pytest.raises(ValueError):
            address(url)


def test_cli_saves_the_actual_address_and_absolute_credential_path(tmp_path, monkeypatch):
    from sandweave.weave.client import cluster_config
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'client'))
    cluster = Cluster.start('origin', directory=tmp_path / 'controller', local_worker=False)
    url = cluster.info['connection']['address']
    try:
        result = subprocess.check_output([sys.executable, '-m', 'sandweave.cli', 'cluster', 'connect',
            'remote', url, '--token-file', 'controller/credentials.json'], cwd=tmp_path, text=True)
        assert json.loads(result)['address'] == url
        assert cluster_config('remote')['token_file'] == str(tmp_path / 'controller/credentials.json')
        with Cluster.connect('remote') as remote:
            assert remote.info['id'] == cluster.info['id']
    finally:
        cluster.stop(); cluster.close()


def test_worker_caps_narrow_affinity_and_preserve_parent_process(tmp_path):
    eligible = sorted(os.sched_getaffinity(0))
    environment = {**os.environ, 'SANDWEAVE_MEMORY_BUDGET': '4GiB'}
    command = 'from sandweave.weave.worker import limits; import os,json; limits(1,0,"8GiB"); print(json.dumps([sorted(os.sched_getaffinity(0)),os.environ["SANDWEAVE_GPU_LIMIT"],os.environ["SANDWEAVE_MEMORY_BUDGET"]]))'
    result = subprocess.check_output([sys.executable, '-c', command], text=True, env=environment)
    cpus, devices, memory = json.loads(result)
    assert cpus == eligible[:1]
    assert devices == ''
    assert memory == str(4*1024**3) + 'B'
    assert sorted(os.sched_getaffinity(0)) == eligible


def test_gpu_cap_intersects_the_existing_slurm_allocation(monkeypatch):
    from sandweave.sandbox.workspace import engine_sources
    import importlib.util
    spec = importlib.util.spec_from_file_location('test_gpu_caps', engine_sources() / 'gvisor_gpu.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv('SLURM_STEP_GPUS', '1,3')
    monkeypatch.setenv('SANDWEAVE_GPU_LIMIT', '3,9')
    assert module.eligible_devices() == [3]
    monkeypatch.setenv('SANDWEAVE_GPU_LIMIT', '')
    assert module.eligible_devices() == []
    monkeypatch.delenv('SANDWEAVE_GPU_LIMIT')
    assert module.eligible_devices() == [1, 3]


def test_printed_join_commands_authenticate_without_a_saved_target(tmp_path, monkeypatch, capsys):
    import shlex
    import ipaddress
    from urllib.parse import urlsplit
    from sandweave.cli import main
    from sandweave.weave import worker as agent
    from sandweave.sandbox import workspace
    master, client = tmp_path / 'master', tmp_path / 'new-worker'
    master.mkdir(); client.mkdir()
    monkeypatch.chdir(master)
    monkeypatch.delenv('SANDWEAVE_HOME', raising=False)
    monkeypatch.delenv('SANDWEAVE_TOKEN', raising=False)
    monkeypatch.delenv('SANDWEAVE_TOKEN_FILE', raising=False)
    assert main(['cluster', 'start', 'demo', '--no-worker',
                 '--directory', str(master / 'state with spaces')]) == 0
    output = capsys.readouterr().out
    commands = [shlex.split(line.strip()) for line in output.splitlines()
                if line.strip().startswith('sandweave cluster join ')]
    assert len(commands) == 2
    with Cluster.connect('demo') as owner:
        try:
            expected = owner.info['id']
            url = owner.info['connection']['address']
            # The default actually accepts non-loopback connections, without
            # --transport or --listen and without a preconfigured client.
            addresses = socket.getaddrinfo(urlsplit(url).hostname, urlsplit(url).port,
                                           socket.AF_INET, socket.SOCK_STREAM)
            host = next(item[4][0] for item in addresses if not ipaddress.ip_address(item[4][0]).is_loopback)
            from sandweave.weave.transport import join_link
            with Cluster.connect(join_link('http://' + host + ':' + str(urlsplit(url).port), owner.connection.token)) as peer:
                assert peer.info['id'] == expected
            dashboard = next(line.removeprefix('Dashboard: ') for line in output.splitlines() if line.startswith('Dashboard: '))
            assert dashboard.startswith(url + '/dashboard/#token=')
            assert 'HTTP: ' + commands[0][-1] in output
            assert 'SSH: ' + commands[1][-1] in output
            assert '--transport' not in output
            monkeypatch.chdir(client)
            assert workspace.home() == client / '.sandweave'
            calls = []
            def start(config, **kwargs):
                # Exercise authentication against the actual controller. Only
                # runtime installation/agent launch is replaced in this test.
                from sandweave.weave.client import ClusterConnection
                connection = ClusterConnection(config)
                try:
                    assert connection.call('status')['id'] == expected
                finally:
                    connection.close()
                calls.append(kwargs)
                return {'state': 'preparing'}
            monkeypatch.setattr(agent, 'start', start)
            for command in commands:
                assert main(command[1:]) == 0
            assert len(calls) == 2
            assert all(call['cpus'] is None and call['gpus'] is None for call in calls)
            assert not (client / '.sandweave/config.json').exists()
            assert main(['cluster', 'join', commands[0][-1] + 'wrong']) == 1
            assert len(calls) == 2  # Bad credentials cannot start installation.
        finally:
            owner.stop()


def test_default_controllers_choose_distinct_ports_and_keep_them_on_restart(tmp_path, monkeypatch):
    from sandweave.sandbox.ownership import process_alive
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path))
    with Cluster.start('first', local_worker=False) as first:
        try:
            with Cluster.start('second', local_worker=False) as second:
                try:
                    original = first.info['connection']['address']
                    assert original != second.info['connection']['address']
                    first.stop()
                    info = json.loads((tmp_path / 'clusters/first/controller.json').read_text())
                    deadline = time.monotonic() + 5
                    while process_alive(info['process']) is not False:
                        assert time.monotonic() < deadline
                        time.sleep(.02)
                    with Cluster.start('first', local_worker=False) as restarted:
                        try:
                            assert restarted.info['connection']['address'] == original
                        finally:
                            restarted.stop()
                finally:
                    second.stop()
        finally:
            try:
                first.stop()
            except OperationUnknown:
                pass


def test_explicit_loopback_prints_its_url_without_claiming_remote_http(tmp_path, monkeypatch, capsys):
    from sandweave.weave.cli import instructions
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path))
    with Cluster.start('private', local_worker=False, listen='127.0.0.1:0') as cluster:
        try:
            instructions(cluster)
            output = capsys.readouterr().out
            assert 'HTTP (this machine only): http://127.0.0.1:' in output
            assert 'Dashboard (this machine only): http://127.0.0.1:' in output
            assert 'sandweave cluster join ssh://' in output
            assert 'sandweave cluster join http://' not in output
            assert '--transport' not in output
        finally:
            cluster.stop()


def test_certificates_enable_printed_https_and_ssh_without_transport_flags(tmp_path, monkeypatch, capsys):
    from sandweave.cli import main
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path))
    cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
        '-subj', '/CN=' + socket.gethostname(), '-addext', 'subjectAltName=DNS:' + socket.gethostname(),
        '-keyout', str(key), '-out', str(cert)], check=True, capture_output=True)
    assert main(['cluster', 'start', 'secure', '--no-worker', '--tls-cert', str(cert), '--tls-key', str(key)]) == 0
    output = capsys.readouterr().out
    with Cluster.connect('secure') as cluster:
        try:
            link = next(line.removeprefix('HTTPS: ') for line in output.splitlines() if line.startswith('HTTPS: '))
            assert 'Dashboard: https://' in output
            assert 'SSH: ssh://' in output
            assert 'HTTP: ' not in output
            with Cluster.connect(link, ca_file=cert) as peer:
                assert peer.info['id'] == cluster.info['id']
            # An external HTTPS listener still provides plain loopback RPC
            # for the simultaneously advertised SSH connection.
            from sandweave.weave.client import metadata
            info = metadata(cluster.config)
            assert info['local_port'] != info['port']
        finally:
            cluster.stop()


def test_join_fragment_never_enters_http_path_and_is_validated():
    from sandweave.weave.transport import join_link
    link = join_link('https://master.example/weave', 'private+value/&=')
    parsed = address(link)
    assert parsed == {'url': 'https://master.example/weave', 'token': 'private+value/&='}
    for value in ('http://host#token=', 'http://host#token=a&token=b',
                  'http://host#token=a&other=b', 'http://host#nonsense', 'ssh://host/state#token=x'):
        with pytest.raises(ValueError):
            address(value)


def test_transport_errors_do_not_start_a_controller(tmp_path, monkeypatch):
    from sandweave.cli import main
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path))
    monkeypatch.setattr(Cluster, 'start', lambda *a, **k: pytest.fail('invalid options started a controller'))
    assert main(['cluster', 'start', '--transport', 'https']) == 1
    assert main(['cluster', 'start', '--transport', 'ssh', '--listen', '0.0.0.0:8765']) == 1
    assert main(['cluster', 'start', '--advertise', 'http://0.0.0.0:8765']) == 1

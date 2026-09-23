"""Focused public service-network acceptance; no benchmark or build suite."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shlex
import time

import pytest

from sandweave import Sandbox, Memory, Network, create_service_network, delete_service_network
from sandweave.sandbox.errors import ResourceUnavailable
from test_weave_live import cluster

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit disposable workers required')]


def start_server(env):
    env.files.write_text('/tmp/server.py', '''from http.server import BaseHTTPRequestHandler,HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data=self.client_address[0].encode()
        self.send_response(200); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self,*args): pass
server=HTTPServer(('0.0.0.0',8123),Handler)
print('ready',flush=True)
server.serve_forever()
''')
    process = env.exec('python3 /tmp/server.py')
    assert process.stdout.readline() == 'ready\n'
    return process


def fetch(env, host, *, success=True):
    script = ('import urllib.request; print(urllib.request.urlopen(' +
              repr('http://' + host + ':8123') + ',timeout=1).read().decode())')
    result = env.run('python3 -c ' + shlex.quote(script), timeout=4)
    if success:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
    return result.stdout.strip()


def exercise(target=None):
    identity = create_service_network(target=target)
    other = create_service_network(target=target)
    envs = []
    options = dict(target=target, service_network=identity, cpu=1,
                   memory=Memory('256MiB', '128MiB'), network='offline')
    try:
        client = Sandbox(**options, aliases=['client'], networks=['front'])
        envs.append(client)
        # The client relay starts before these aliases exist.
        def launch(index):
            return Sandbox(**options, aliases=['api-' + str(index)], networks=['front'])
        with ThreadPoolExecutor(3) as executor:
            futures = [executor.submit(launch, index) for index in range(3)]
            failures = []
            for future in futures:
                try:
                    envs.append(future.result())
                except Exception as error:
                    failures.append(error)
            if failures:
                raise failures[0]
        for env in envs[1:]:
            start_server(env)
        source = client.info['service_network']['address']
        with ThreadPoolExecutor(3) as executor:
            assert list(executor.map(lambda index: fetch(client, 'api-' + str(index)), range(3))) == [source] * 3
        hidden = Sandbox(**options, aliases=['private'], networks=['back'])
        envs.append(hidden)
        start_server(hidden)
        fetch(client, hidden.info['service_network']['address'], success=False)
        isolated = Sandbox(**{**options, 'service_network': other}, aliases=['other'], networks=['front'])
        envs.append(isolated)
        fetch(isolated, envs[1].info['service_network']['address'], success=False)
        assert client.run("python -c 'import socket; socket.create_connection((\"1.1.1.1\",443),1)'",
                          timeout=4).returncode != 0
        with pytest.raises(ResourceUnavailable):
            delete_service_network(identity, target=target)
        # Existing members remain responsive while peers leave and rejoin.
        envs[1].terminate()
        envs[1].close()
        replacement = Sandbox(**options, aliases=['api-0'], networks=['front'])
        envs.append(replacement)
        start_server(replacement)
        assert fetch(client, 'api-0') == source
        assert replacement.info['service_network']['address'] != envs[1]._info['spec']['_service_network']['address']
        if target is not None:
            from sandweave.weave.client import Cluster
            owned = {env.id for env in envs if env is not isolated}
            controller = Cluster.connect(target)
            try:
                rows = controller.info['sandboxes']
            finally:
                controller.close()
            assert len({row['worker'] for row in rows if row['id'] in owned}) == 1
        print(json.dumps({'network': identity, 'members': len(envs), 'source': source,
                          'isolation': 'passed', 'late_aliases': 'passed'}))
    finally:
        for env in reversed(envs):
            env.terminate()
            env.close()
        if target is not None:
            from sandweave.weave.client import Cluster
            controller = Cluster.connect(target)
            try:
                deadline = time.monotonic() + 15
                while any(not row['released'] for row in controller.info['sandboxes']):
                    if time.monotonic() > deadline:
                        raise TimeoutError('controller did not release service network members')
                    time.sleep(.05)
            finally:
                controller.close()
        delete_service_network(identity, target=target)
        delete_service_network(other, target=target)


def test_local_incremental_service_network():
    exercise()


@pytest.mark.skipif(not os.environ.get('SANDWEAVE_WEAVE_INTEGRATION'), reason='explicit cluster required')
def test_http_service_network_placement_and_isolation(cluster):
    from sandweave.weave.transport import join_link
    target = join_link(cluster.info['connection']['address'], cluster.connection.token)
    exercise(target)


def test_public_dns_keeps_the_normal_egress_policy():
    identity = create_service_network()
    env = None
    try:
        env = Sandbox(service_network=identity, memory=Memory('256MiB', '128MiB'),
                      network=Network('allowlist', allowed_hosts=('example.com',)))
        result = env.run("python3 -c 'import urllib.request; print(urllib.request.urlopen(\"https://example.com\",timeout=15).status)'",
                         timeout=20)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == '200'
        assert env.run("python3 -c 'import socket; socket.create_connection((\"1.1.1.1\",443),1)'",
                       timeout=4).returncode != 0
    finally:
        if env:
            env.terminate()
            env.close()
        delete_service_network(identity)


def test_filesystem_restore_registers_a_fresh_member():
    identity = create_service_network()
    builder = restored = None
    options = dict(service_network=identity, memory=Memory('256MiB', '128MiB'), network='offline')
    try:
        builder = Sandbox(**options, aliases=['builder'])
        builder.files.write_text('/workspace/saved.txt', 'retained')
        snapshot = builder.snapshot(state='filesystem')
        restored = Sandbox(cache=snapshot, **options, aliases=['restored'])
        assert restored.files.read_text('/workspace/saved.txt') == 'retained'
        assert builder.info['service_network']['address'] != restored.info['service_network']['address']
        start_server(builder)
        assert fetch(restored, 'builder') == restored.info['service_network']['address']
    finally:
        for env in (restored, builder):
            if env:
                env.terminate()
                env.close()
        delete_service_network(identity)


@pytest.mark.skipif(not os.environ.get('SANDWEAVE_NETWORK_DESKTOP'), reason='explicit desktop acceptance required')
def test_custom_gnome_template_joins_an_existing_network(monkeypatch):
    identity = create_service_network()
    server = desktop = None
    try:
        server = Sandbox(service_network=identity, aliases=['api'], network='offline',
                         memory=Memory('256MiB', '128MiB'))
        start_server(server)
        # Optional prepared desktop inputs exercise a different immutable
        # worker workspace in the same allocation, after the hub is running.
        with monkeypatch.context() as inputs:
            if os.environ.get('SANDWEAVE_NETWORK_DESKTOP_ASSETS'):
                inputs.setenv('SANDWEAVE_ASSETS', os.environ['SANDWEAVE_NETWORK_DESKTOP_ASSETS'])
            desktop = Sandbox(template={'extends': 'gnome', 'name': 'network-desktop'}, cpu=2,
                              memory=Memory('4GiB', '512MiB'), network='offline',
                              service_network=identity, aliases=['desktop'])
        assert fetch(desktop, 'api') == desktop.info['service_network']['address']
        image = desktop.desktop.screenshot()
        destination = Path(os.environ['SANDWEAVE_NETWORK_DESKTOP'])
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination)
        print(json.dumps({'screenshot': str(destination), 'network': desktop.info['service_network'],
                          'timings': desktop.timings}))
    finally:
        if desktop:
            desktop.terminate()
            desktop.close()
        if server:
            server.terminate()
            server.close()
        delete_service_network(identity)

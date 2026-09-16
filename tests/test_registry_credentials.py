import base64
import io
import json
from urllib.error import HTTPError

import pytest

from sandweave.templates.credentials import build_configuration, credentials
from sandweave.templates.registry import Registry


def test_auth_is_scoped_to_its_registry_and_helpers_are_materialized(tmp_path, monkeypatch):
    monkeypatch.setenv('DOCKER_CONFIG', str(tmp_path))
    (tmp_path / 'config.json').write_text(json.dumps({'auths': {
        'https://index.docker.io/v1/': {'auth': 'docker-secret'},
        'private.test': {'auth': 'private-secret'}}, 'credHelpers': {'helper.test': 'test'}}))
    calls = []
    def helper(name, operation, value=None):
        calls.append((name, operation, value))
        return {'Username': 'user', 'Secret': 'password'}
    monkeypatch.setattr('sandweave.templates.credentials.helper', helper)
    assert credentials('registry-1.docker.io') == {'auth': 'docker-secret'}
    assert credentials('unrelated.test') == {}
    resolved = build_configuration()
    assert set(resolved['auths']) == {'https://index.docker.io/v1/', 'private.test', 'helper.test'}
    assert base64.b64decode(resolved['auths']['helper.test']['auth']) == b'user:password'
    assert calls == [('test', 'get', 'helper.test')]


def test_global_store_keeps_anonymous_pulls_anonymous(monkeypatch):
    calls = []
    def helper(name, operation, value=None):
        calls.append(operation)
        return {'private.test': 'user'}
    monkeypatch.setattr('sandweave.templates.credentials.helper', helper)
    assert credentials('public.test', {'credsStore': 'test'}) == {}
    assert calls == ['list']


@pytest.mark.parametrize('kind', ['Basic', 'Bearer'])
def test_registry_authentication_retries_with_only_selected_credentials(tmp_path, monkeypatch, kind):
    registry = Registry('docker://private.test/org/image', tmp_path)
    monkeypatch.setattr('sandweave.templates.registry.credentials', lambda host: {'auth': 'selected-secret'})
    seen = []
    def open(request, timeout):
        seen.append(request)
        if len(seen) == 1:
            raise HTTPError(request.full_url, 401, 'auth required', {'WWW-Authenticate':
                kind + ' realm="https://auth.private.test/token",service="private.test"'}, None)
        if request.full_url.startswith('https://auth.private.test'):
            assert request.get_header('Authorization') == 'Basic selected-secret'
            return io.BytesIO(b'{"token":"pull-token"}')
        assert request.get_header('Authorization') == (
            'Basic selected-secret' if kind == 'Basic' else 'Bearer pull-token')
        return io.BytesIO(b'content')
    monkeypatch.setattr(registry.opener, 'open', open)
    assert registry.open('/manifests/latest').read() == b'content'
    assert not seen[0].has_header('Authorization')


def test_token_refresh_uses_post_without_putting_secret_in_url(tmp_path, monkeypatch):
    registry = Registry('docker://private.test/image', tmp_path)
    monkeypatch.setattr('sandweave.templates.registry.credentials', lambda host: {'identitytoken': 'refresh-secret'})
    requests = []
    def open(request, timeout):
        requests.append(request)
        if len(requests) == 1:
            raise HTTPError(request.full_url, 401, '', {'WWW-Authenticate':
                'Bearer realm="https://auth.private.test/token"'}, None)
        if len(requests) == 2:
            assert request.get_method() == 'POST'
            assert b'refresh_token=refresh-secret' in request.data
            assert 'refresh-secret' not in request.full_url
            return io.BytesIO(b'{"access_token":"access"}')
        assert request.get_header('Authorization') == 'Bearer access'
        return io.BytesIO(b'ok')
    monkeypatch.setattr(registry.opener, 'open', open)
    assert registry.open('/manifests/latest').read() == b'ok'


def test_private_registry_pull_over_real_tls(tmp_path, monkeypatch):
    import hashlib
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import shutil
    import ssl
    import subprocess
    import threading
    if not shutil.which('openssl'):
        pytest.skip('openssl is needed to create the disposable registry certificate')
    cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-keyout', str(key), '-out', str(cert), '-days', '1', '-subj', '/CN=localhost',
                    '-addext', 'subjectAltName=IP:127.0.0.1,DNS:localhost'],
                   check=True, capture_output=True)
    config = json.dumps({'os': 'linux', 'architecture': 'amd64',
                         'rootfs': {'type': 'layers', 'diff_ids': []}}).encode()
    descriptor = {'digest': 'sha256:' + hashlib.sha256(config).hexdigest(), 'size': len(config)}
    manifest = json.dumps({'schemaVersion': 2, 'config': descriptor, 'layers': []}).encode()
    token = base64.b64encode(b'acceptance:disposable-password').decode()
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            authorized = self.headers.get('Authorization') == 'Basic ' + token
            requests.append((self.path, authorized))
            if not authorized:
                self.send_response(401)
                self.send_header('WWW-Authenticate', 'Basic realm="test-registry"')
                self.end_headers()
                return
            body = config if '/blobs/' in self.path else manifest
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    registry_host = '127.0.0.1:' + str(server.server_port)
    (tmp_path / 'config.json').write_text(json.dumps({'auths': {registry_host: {'auth': token}}}))
    monkeypatch.setenv('DOCKER_CONFIG', str(tmp_path))
    monkeypatch.setenv('SSL_CERT_FILE', str(cert))
    monkeypatch.setenv('NO_PROXY', '127.0.0.1')
    try:
        registry = Registry('docker://' + registry_host + '/private/image', tmp_path / 'blobs')
        resolved = registry.resolve()
        assert resolved['digest'] == 'sha256:' + hashlib.sha256(manifest).hexdigest()
        assert resolved['config']['architecture'] == 'amd64'
        assert requests == [('/v2/private/image/manifests/latest', False),
                            ('/v2/private/image/manifests/latest', True),
                            ('/v2/private/image/blobs/' + descriptor['digest'], True)]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

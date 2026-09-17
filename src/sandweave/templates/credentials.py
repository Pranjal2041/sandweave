"""Read the caller's Docker credential configuration without logging secrets."""
import base64
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlsplit


def registry_name(value):
    parsed = urlsplit(value if '://' in value else 'https://' + value)
    host = parsed.netloc.lower()
    return 'registry-1.docker.io' if host in ('docker.io', 'index.docker.io') else host


def configuration():
    directory = Path(os.environ.get('DOCKER_CONFIG', str(Path.home() / '.docker')))
    path = directory / 'config.json'
    return json.loads(path.read_text()) if path.is_file() else {}


def helper(name, operation, value=None):
    if not name or '/' in name or '\\' in name:
        raise ValueError('invalid Docker credential helper name')
    result = subprocess.run(['docker-credential-' + name, operation], input=value,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if result.returncode:
        # Helpers can include credential contents in error messages.
        raise ValueError('Docker credential helper failed: ' + name + ' ' + operation)
    return json.loads(result.stdout)


def credentials(host, config=None):
    config = configuration() if config is None else config
    host = registry_name(host)
    helpers = config.get('credHelpers') or {}
    records = config.get('auths') or {}
    keys = [*helpers, *records]
    if config.get('credsStore') and not any(registry_name(key) == host for key in keys):
        keys.extend(helper(config['credsStore'], 'list'))
    key = next((key for key in keys if registry_name(key) == host), None)
    if key is None:
        return {}
    selected = helpers.get(key) or config.get('credsStore')
    if selected:
        value = helper(selected, 'get', key)
        user, secret = value.get('Username'), value.get('Secret')
        return {'identitytoken': secret} if user == '<token>' else {
            'auth': base64.b64encode((user + ':' + secret).encode()).decode()}
    record = records.get(key) or {}
    return {key: record[key] for key in ('auth', 'identitytoken') if record.get(key)}


def build_configuration():
    """Materialize helper-backed auth for the isolated BuildKit client only."""
    config = configuration()
    registries = set(config.get('auths') or {}) | set(config.get('credHelpers') or {})
    if config.get('credsStore'):
        registries.update(helper(config['credsStore'], 'list'))
    auths = {}
    for registry in registries:
        value = credentials(registry, config)
        if value:
            auths[registry] = value
    return {'auths': auths}

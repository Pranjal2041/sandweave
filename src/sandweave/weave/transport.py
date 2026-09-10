"""Explicit controller addresses and authenticated RPC over HTTP, TLS or SSH."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, unquote, parse_qsl, urlencode

from ..sandbox.connection import Connection


def address(value):
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https', 'ssh') or not parsed.hostname:
        raise ValueError('controller address must use http://, https:// or ssh://')
    if parsed.query or parsed.password:
        raise ValueError('controller addresses cannot contain a query or password')
    if parsed.scheme == 'ssh':
        if parsed.fragment:
            raise ValueError('SSH addresses cannot contain a fragment')
        if not parsed.path or parsed.path == '/':
            raise ValueError('SSH controller address needs its absolute state directory: ssh://host/path/to/controller')
        return {'hostname': parsed.hostname,
                'ssh_host': (parsed.username + '@' if parsed.username else '') + parsed.hostname,
                'ssh_port': parsed.port, 'directory': unquote(parsed.path), 'forward': True}
    if parsed.username:
        raise ValueError('provide the controller token separately from its address')
    # Validate the port now, before saving an unusable target.
    parsed.port
    config = {'url': parsed._replace(fragment='').geturl().rstrip('/')}
    if parsed.fragment:
        try:
            fields = parse_qsl(parsed.fragment, strict_parsing=True, keep_blank_values=True)
        except ValueError:
            raise ValueError('Invalid join link; copy the complete link printed by cluster start') from None
        if len(fields) != 1 or fields[0][0] != 'token':
            raise ValueError('Invalid join link; copy the complete link printed by cluster start')
        config['token'] = fields[0][1]
        credential(config)
    return config


def join_link(url, token):
    """Keep the credential in a fragment, never in the HTTP request path."""
    config = address(url)
    if 'url' not in config or 'token' in config:
        raise ValueError('Expected a plain HTTP or HTTPS controller address')
    credential({'token': token})
    return config['url'] + '#' + urlencode({'token': token})


def credential(config):
    token = config.get('token')
    path = config.get('token_file')
    if token is None and path is None:
        token = os.environ.get('SANDWEAVE_TOKEN')
        path = os.environ.get('SANDWEAVE_TOKEN_FILE')
    if token is None and path:
        token = Path(path).expanduser().read_text().strip()
        if token.startswith('{'):
            token = json.loads(token)['token']
    if not isinstance(token, str) or not token or any(not 33 <= ord(c) <= 126 for c in token):
        raise ValueError('controller token is missing or invalid; set SANDWEAVE_TOKEN_FILE or pass token_file')
    return token


def connect(config, *, timeout=300):
    parsed = urlsplit(config['url'])
    address(config['url'])
    return Connection(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80),
        credential(config), timeout=timeout, tls=parsed.scheme == 'https',
        ca_file=config.get('ca_file') or os.environ.get('SANDWEAVE_CA_FILE'),
        rpc_path=parsed.path.rstrip('/') + '/rpc')


class ForwardedConnection:
    """A client uses the controller it already reached, without opening a worker socket."""
    def __init__(self, connection, route):
        self.control, self.route = connection, route

    def call(self, operation, **parameters):
        return self.control.call('sandbox_rpc', identity=self.route['id'],
                                 method=operation, parameters=parameters)

    def close(self):
        pass  # The owning ClusterConnection closes this shared transport.

"""Bind one proxy for a sandbox's lifetime, retaining it across live restores."""
import ipaddress
import json
from pathlib import Path
import secrets
import socket

from ... import proxy
from ...workspace import atomic_json


def bind(root, identity, network, snapshot):
    if network['mode'] != 'proxy':
        return None, ['--clear-proxy']
    urls = proxy.values(network['proxy'])
    saved = None
    if snapshot:
        manifest = json.loads((Path(snapshot) / 'snapshot-manifest.json').read_text())
        if manifest['kind'] == 'live':
            saved = json.loads((Path(snapshot) / 'launch-settings.json').read_text())['settings']
    index = saved['proxy_index'] if saved else secrets.randbelow(len(urls))
    if not isinstance(index, int) or not 0 <= index < len(urls):
        raise ValueError('saved proxy selection is incompatible with the requested proxy list')
    _, hostname, port = proxy.parse(urls[index])
    if saved:
        addresses = [endpoint.rsplit(':', 1)[0] for endpoint in saved['proxy_endpoints']]
    else:
        try:
            addresses = sorted({item[4][0] for item in socket.getaddrinfo(
                hostname, port, family=socket.AF_INET, type=socket.SOCK_STREAM)})
        except OSError:
            raise ValueError('could not resolve the configured proxy hostname') from None
    if not addresses or any(not ipaddress.IPv4Address(address).is_global for address in addresses):
        raise ValueError('proxy endpoints must resolve to public IPv4 addresses')
    selected = {'index': index, 'url': urls[index], 'host': hostname, 'addresses': addresses,
                'public': {'mode': 'proxy', 'proxy': proxy.public(urls[index]), 'addresses': addresses}}
    logs = root / 'runs/gvisor' / identity
    logs.mkdir(parents=True, exist_ok=True)
    atomic_json(logs / 'proxy.json', selected['public'])
    options = ['--proxy-index', str(index)]
    for address in addresses:
        options += ['--proxy-endpoint', f'{address}:{port}']
    return selected, options


def configure(client, selected):
    if selected is None:
        return {}
    content = client.call('file', op='read', path='/etc/hosts', offset=0, size=1024**2)
    replaced = proxy.hosts_file(content, selected['host'], selected['addresses'])
    if content != replaced:
        client.call('file', op='write', path='/etc/hosts', data=replaced, offset=0, truncate=True)
    return {'proxy_env': proxy.environment(selected['url'])}

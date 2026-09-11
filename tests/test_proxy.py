import importlib.util
import json
from pathlib import Path
import socket

import pytest

from sandweave import Network
from sandweave.sandbox import proxy
from sandweave.sandbox.resources import normalize
from sandweave.sandbox.runtimes.gvisor.proxy import bind


def test_proxy_values_hide_credentials_and_preserve_default_contract():
    value = Network(proxy=['http://alice:private@1.1.1.1:1234', 'socks5h://8.8.8.8:1080'])
    assert value.mode == 'proxy'
    assert 'private' not in repr(value)
    assert normalize()['network'] == {'mode': 'internet', 'allow_cidrs': ()}
    normalized = normalize(network=value)
    visible = proxy.public_resources(normalized)
    assert visible['network']['proxy'] == ['http://1.1.1.1:1234', 'socks5h://8.8.8.8:1080']
    assert 'private' in normalized['network']['proxy'][0]
    assert normalize(network=normalized['network']) == normalized


@pytest.mark.parametrize('url', ['ftp://secret@1.1.1.1', 'http://1.1.1.1:0',
    'http://1.1.1.1:65536', 'http://[::1]', 'http://proxy/path', 'http://proxy?secret=foo',
    'http://proxy\n', 'http://', 'http://secret:private@proxy:bad'])
def test_invalid_proxy_does_not_echo_credentials(url):
    with pytest.raises(ValueError) as error:
        Network(proxy=url)
    if '@' in url:
        assert url not in str(error.value)
    assert 'private' not in str(error.value)


def test_network_modes_cannot_silently_bypass_proxy():
    for args in [dict(mode='offline', proxy='http://1.1.1.1'), dict(mode='proxy'),
                 dict(proxy=[]), dict(proxy='http://1.1.1.1', allow_cidrs=('8.8.8.8/32',))]:
        with pytest.raises(ValueError):
            Network(**args)


def test_proxy_hosts_preserve_unrelated_aliases():
    original = b'127.0.0.1 localhost\n1.2.3.4 proxy.example keep\n2.3.4.5 old.example # sandweave proxy endpoint\n'
    replaced = proxy.hosts_file(original, 'proxy.example', ['8.8.8.8'])
    assert b'127.0.0.1 localhost' in replaced and b'1.2.3.4 keep' in replaced
    assert b'old.example' not in replaced
    assert b'8.8.8.8 proxy.example # sandweave proxy endpoint' in replaced


def test_proxy_binding_is_private_and_preserved_on_live_restore(tmp_path, monkeypatch):
    from sandweave.sandbox.runtimes.gvisor import proxy as runtime
    monkeypatch.setattr(runtime.secrets, 'randbelow', lambda _: 1)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(2, 1, 6, '', ('8.8.8.8', 8080))])
    network = normalize(network=Network(proxy=['http://1.1.1.1:8080', 'http://a:private@proxy.example:8080']))['network']
    selected, options = bind(tmp_path, 'first', network, None)
    assert selected['index'] == 1
    assert 'private' not in ' '.join(options)
    public = (tmp_path / 'runs/gvisor/first/proxy.json').read_text()
    assert 'private' not in public and 'proxy.example' in public
    snapshot = tmp_path / 'snapshot'; snapshot.mkdir()
    (snapshot / 'snapshot-manifest.json').write_text('{"kind":"live"}')
    (snapshot / 'launch-settings.json').write_text(json.dumps({'settings': {
        'proxy_index': 1, 'proxy_endpoints': ['8.8.8.8:8080']}}))
    monkeypatch.setattr(runtime.secrets, 'randbelow', lambda _: pytest.fail('live restore must preserve its proxy'))
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: pytest.fail('live binding is already pinned'))
    restored, restored_options = bind(tmp_path, 'restored', network, snapshot)
    assert restored == selected and options == restored_options
    assert bind(tmp_path, 'offline', {'mode': 'offline'}, snapshot) == (None, ['--clear-proxy'])


def test_proxy_firewall_blocks_direct_dns_udp_and_other_ports(monkeypatch):
    scripts = Path(__file__).parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('proxy_policy_test', scripts / 'test-network-policy.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    policy = module.NetworkPolicy({'mode': 'proxy', 'guest': '10.0.2.15', 'gateway': '10.0.2.2',
        'dns': '10.0.2.3', 'host_addresses': ['8.9.10.11'], 'forwarded_tcp_ports': [23799],
        'proxy_endpoints': ['1.1.1.1:8080']})
    assert policy.allow(module.packet(dst='1.1.1.1', dport=8080), 'to-passt')
    for dst, port, proto in [('1.1.1.1',443,6),('1.1.1.1',8080,17),('8.8.8.8',443,6),
                              ('10.0.2.3',53,17),('10.0.2.2',8080,6),('169.254.169.254',80,6)]:
        assert not policy.allow(module.packet(dst=dst,dport=port,proto=proto), 'to-passt')
    policy.allow(module.packet(src='10.0.2.2',dst='10.0.2.15',sport=41000,dport=23799),'from-passt')
    assert policy.allow(module.packet(dst='10.0.2.2',sport=23799,dport=41000,flags=0x12),'to-passt')

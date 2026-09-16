import ipaddress
from pathlib import Path
import struct
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from network_allowlist import Allowlist, dns_name
from network_policy import NetworkPolicy
from test_service_network import frame


def name(value):
    return b''.join(bytes([len(part)]) + part.encode() for part in value.split('.')) + b'\0'


def messages(host='sub.example.org', ttl=60):
    question = name(host) + struct.pack('!HH', 1, 1)
    query = struct.pack('!6H', 42, 0x100, 1, 0, 0, 0) + question
    response = struct.pack('!6H', 42, 0x8180, 1, 1, 0, 0) + question
    response += b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, ttl, 4) + ipaddress.IPv4Address('93.184.215.14').packed
    return query, response


def test_matching_dns_answers_require_query_and_cannot_add_unrelated_hosts(monkeypatch):
    policy = Allowlist(['*.example.org'], resolve=False)
    query, response = messages()
    address = ipaddress.IPv4Address('93.184.215.14').packed
    policy.observe(response, 10000, response=True)
    assert not policy.allows(address)
    policy.observe(query, 10000, response=False)
    policy.observe(response, 10001, response=True)
    assert not policy.allows(address)
    policy.observe(response, 10000, response=True)
    assert policy.allows(address)
    monkeypatch.setattr('network_allowlist.time.monotonic', lambda: float('inf'))
    assert not policy.allows(address)
    assert not policy.matches('example.org')
    assert not policy.matches('otherexample.org')


def test_cname_chain_allows_only_reachable_records():
    policy = Allowlist(['allowed.test'], resolve=False)
    query, _ = messages('allowed.test')
    question = query[12:]
    alias = name('cdn.test')
    response = struct.pack('!6H', 42, 0x8180, 1, 3, 0, 0) + question
    response += b'\xc0\x0c' + struct.pack('!HHIH', 5, 1, 60, len(alias)) + alias
    response += alias + struct.pack('!HHIH', 1, 1, 60, 4) + b'\x01\x01\x01\x01'
    response += name('unrelated.test') + struct.pack('!HHIH', 1, 1, 60, 4) + b'\x08\x08\x08\x08'
    policy.observe(query, 10000, response=False)
    policy.observe(response, 10000, response=True)
    assert policy.allows(b'\x01\x01\x01\x01')
    assert not policy.allows(b'\x08\x08\x08\x08')


def test_dns_parser_rejects_cycles_truncation_and_corrupt_answers():
    for data in (b'\xc0\0', b'\xc0', b'\x03a'):
        with pytest.raises(ValueError):
            dns_name(data, 0)
    policy = Allowlist(['*.example.org'], resolve=False)
    query, response = messages()
    for index in range(len(response)):
        policy.observe(query, 10000, response=False)
        policy.observe(response[:index], 10000, response=True)
    assert not policy.allows(ipaddress.IPv4Address('93.184.215.14').packed)


def test_packet_policy_blocks_other_destinations_and_applies_phase_changes():
    config = dict(mode='allowlist', allowed_hosts=['1.1.1.1'], guest='10.0.2.15', gateway='10.0.2.2',
                  dns='10.0.2.3', host_addresses=['10.0.0.1'], forwarded_tcp_ports=[23799])
    policy = NetworkPolicy(config)
    assert policy.allow(frame('1.1.1.1', protocol=6), 'to-passt')
    assert not policy.allow(frame('8.8.8.8', protocol=6), 'to-passt')
    policy.update({**config, 'mode': 'offline'})
    assert not policy.allow(frame('1.1.1.1', protocol=6), 'to-passt')
    policy.update({**config, 'mode': 'internet', 'allowed_hosts': []})
    assert policy.allow(frame('8.8.8.8', protocol=6), 'to-passt')


def test_explicit_cidr_and_invalid_entries():
    policy = Allowlist(['1.1.1.0/24'], resolve=False)
    assert policy.allows(b'\x01\x01\x01\x02')
    assert not policy.allows(b'\x01\x01\x02\x02')
    for invalid in ('https://example.com', '*.127.0.0.1:42', 'bad*host', '::1'):
        with pytest.raises(ValueError):
            Allowlist([invalid], resolve=False)


def test_explicit_private_network_does_not_grant_host_access():
    config = dict(mode='allowlist', allowed_hosts=['10.42.0.0/16'], guest='10.0.2.15', gateway='10.0.2.2',
                  dns='10.0.2.3', host_addresses=['10.42.0.1'], forwarded_tcp_ports=[23799])
    policy = NetworkPolicy(config)
    assert policy.allow(frame('10.42.0.2', protocol=6), 'to-passt')
    assert not policy.allow(frame('10.42.0.1', protocol=6), 'to-passt')
    assert not policy.allow(frame('10.43.0.2', protocol=6), 'to-passt')


def test_tcp_dns_handles_segmentation_reordering_and_retransmission():
    def packet(sequence, data=b'', flags=16):
        header = bytearray(20)
        struct.pack_into('!I', header, 4, sequence)
        header[12], header[13] = 5 << 4, flags
        return bytes(header) + data
    policy = Allowlist(['*.example.org'], resolve=False)
    query, response = (struct.pack('!H', len(message)) + message for message in messages())
    policy.observe_tcp(packet(100, flags=2), 10000, response=False)
    policy.observe_tcp(packet(101 + 7, query[7:]), 10000, response=False)
    policy.observe_tcp(packet(101, query[:7]), 10000, response=False)
    policy.observe_tcp(packet(101, query[:7]), 10000, response=False)
    policy.observe_tcp(packet(1000, flags=18), 10000, response=True)
    policy.observe_tcp(packet(1001, response[:1]), 10000, response=True)
    policy.observe_tcp(packet(1002, response[1:]), 10000, response=True)
    assert policy.allows(ipaddress.IPv4Address('93.184.215.14').packed)


def test_tcp_dns_drops_unbounded_or_reset_streams():
    policy = Allowlist([], resolve=False)
    data = bytearray(20)
    data[12] = 5 << 4
    policy.observe_tcp(bytes(data) + b'x' * 131073, 10000, response=False)
    assert not policy.streams
    policy.observe_tcp(bytes(data), 10000, response=False)
    data[13] = 4
    policy.observe_tcp(bytes(data), 10000, response=False)
    assert not policy.streams

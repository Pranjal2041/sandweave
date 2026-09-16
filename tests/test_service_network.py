"""Service routing preserves transport checksums and isolates concurrent groups."""
import importlib.util
import ipaddress
from pathlib import Path
import socket
import struct
import sys
import tempfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from service_network import GUEST, Hub, Peer, checksum, dns_reply, route


def frame(destination, payload=b'payload', *, protocol=17):
    destination = ipaddress.IPv4Address(destination).packed
    segment = (struct.pack('!HHHH', 32100, 8010, 8 + len(payload), 0) + payload if protocol == 17
               else struct.pack('!HHIIBBHHH', 32100, 8010, 9, 7, 0x50, 0x18, 8192, 0, 0) + payload)
    pseudo = GUEST + destination + bytes([0, protocol]) + struct.pack('!H', len(segment))
    value = checksum(pseudo + segment)
    segment = bytearray(segment)
    struct.pack_into('!H', segment, 6 if protocol == 17 else 16, value)
    header = bytearray(struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20 + len(segment), 7, 0, 64,
                                  protocol, 0, GUEST, destination))
    struct.pack_into('!H', header, 10, checksum(header))
    return b'\0' * 12 + b'\x08\x00' + bytes(header) + bytes(segment)


@pytest.mark.parametrize('protocol', [6, 17])
@pytest.mark.parametrize('payload', [b'odd', b'even', bytes(range(255))])
def test_routing_preserves_valid_checksums(protocol, payload):
    source = ipaddress.IPv4Address('10.231.0.2').packed
    result = route(frame('10.231.0.3', payload, protocol=protocol), source, GUEST)
    assert result[26:34] == source + GUEST
    assert checksum(result[14:34]) == 0
    segment = result[34:]
    pseudo = source + GUEST + bytes([0, protocol]) + struct.pack('!H', len(segment))
    assert checksum(pseudo + segment) == 0
    assert result.endswith(payload)


@pytest.mark.parametrize('nested', ['', 'nested-' * 25, '文' * 60])
def test_concurrent_groups_with_identical_private_ips_do_not_cross(nested):
    with tempfile.TemporaryDirectory(prefix='sw-net-') as temporary:
        root = Path(temporary) / nested
        hub = Hub(root)
        peers, guests, packets = [], [], []
        try:
            for group in range(2):
                members = [{'address': '10.231.0.' + str(index + 2),
                            'socket': str(root / f'{group}-{index}.sock'), 'networks': ['default']}
                           for index in range(2)]
                hub.register(str(group), members)
                for member in members:
                    guest, packet = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                    guest.settimeout(.2)
                    guests.append(guest)
                    packets.append(packet)
                    peers.append(Peer({**member, 'hub': str(hub.path),
                                       'peers': {'other': '10.231.0.3'}}, packet))
            assert peers[0].send(frame('10.231.0.3', b'first group'))
            assert peers[2].send(frame('10.231.0.3', b'second group'))
            assert guests[1].recv(9014).endswith(b'first group')
            assert guests[3].recv(9014).endswith(b'second group')
            hub.unregister('0')
            assert peers[0].send(frame('10.231.0.3'))
            with pytest.raises(socket.timeout):
                guests[1].recv(9014)
        finally:
            for peer in peers:
                peer.close()
            for channel in guests + packets:
                channel.close()
            hub.close()


def test_service_dns_is_answered_without_public_dns():
    query = b'\x12\x34\x01\0\0\1\0\0\0\0\0\0\x05redis\0\0\1\0\1'
    request = bytearray(frame('10.0.2.3', query))
    struct.pack_into('!H', request, 36, 53)
    response = dns_reply(request, {'redis': '10.231.0.3'})
    assert response.endswith(ipaddress.IPv4Address('10.231.0.3').packed)
    assert response[42:44] == b'\x12\x34'
    assert checksum(response[14:34]) == 0
    assert dns_reply(request, {'different': '10.231.0.3'}) is None


@pytest.mark.parametrize('nested', ['', 'nested-' * 25, '文' * 60])
def test_router_restart_preserves_existing_guest_connections(nested):
    with tempfile.TemporaryDirectory(prefix='sw-net-') as temporary:
        root = Path(temporary) / nested
        members = [{'address': '10.231.0.' + str(index + 2),
                    'socket': str(root / f'{index}.sock'), 'networks': ['default']}
                   for index in range(2)]
        hub = Hub(root)
        hub.register('group', members)
        channels = [socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET) for _ in members]
        peers = [Peer({**member, 'hub': str(hub.path), 'peers': {'other': '10.231.0.3'}}, pair[1])
                 for member, pair in zip(members, channels)]
        channels[1][0].settimeout(1)
        try:
            peers[0].send(frame('10.231.0.3', b'before'))
            assert channels[1][0].recv(9014).endswith(b'before')
            hub.close()
            peers[0].send(frame('10.231.0.3', b'during'))
            hub = Hub(root)
            hub.register('group', members)
            peers[0].send(frame('10.231.0.3', b'after'))
            assert channels[1][0].recv(9014).endswith(b'after')
        finally:
            for peer in peers:
                peer.close()
            for pair in channels:
                for channel in pair:
                    channel.close()
            hub.close()

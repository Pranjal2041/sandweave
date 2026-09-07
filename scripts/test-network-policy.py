#!/usr/bin/env python3
import ipaddress
import struct
import unittest
from network_policy import NetworkPolicy


def packet(dst='1.1.1.1', src='10.0.2.15', sport=12345, dport=443, flags=2, proto=6):
    payload = struct.pack('!HHIIBBHHH', sport, dport, 1, 1, 0x50, flags, 1000, 0, 0)
    if proto == 17:
        payload = struct.pack('!HHHH', sport, dport, 8, 0)
    ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20 + len(payload), 0, 0, 64, proto, 0,
                     ipaddress.IPv4Address(src).packed, ipaddress.IPv4Address(dst).packed)
    return b'\0' * 12 + b'\x08\x00' + ip + payload


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.p = NetworkPolicy({'mode': 'internet', 'guest': '10.0.2.15', 'gateway': '10.0.2.2',
                                'dns': '10.0.2.3', 'host_addresses': ['10.1.1.79', '8.9.10.11'],
                                'forwarded_tcp_ports': [80, 5901]})

    def allow(self, **kwargs):
        return self.p.allow(packet(**kwargs), 'to-passt')

    def test_public_and_dns(self):
        self.assertTrue(self.allow())
        self.assertTrue(self.allow(dst='10.0.2.3', dport=53, proto=17))
        self.assertTrue(self.allow(dst='10.0.2.3', dport=53))
        self.assertFalse(self.allow(dst='10.0.2.3', dport=80))

    def test_host_private_special_addresses(self):
        for dst in ('10.0.2.2', '127.0.0.1', '127.254.1.2', '10.1.1.79', '172.16.1.79',
                    '192.168.1.1', '169.254.169.254', '100.64.1.2', '8.9.10.11',
                    '0.0.0.0', '224.0.0.1', '255.255.255.255', '240.1.2.3'):
            for proto in (6, 17):
                self.assertFalse(self.allow(dst=dst, proto=proto), dst)

    def test_forward_replies_but_not_new_connections(self):
        self.assertFalse(self.allow(dst='10.0.2.2', sport=80, dport=40000, flags=0x12))
        self.p.allow(packet(src='10.0.2.2', dst='10.0.2.15', sport=40000, dport=80), 'from-passt')
        self.assertTrue(self.allow(dst='10.0.2.2', sport=80, dport=40000, flags=0x12))
        self.assertTrue(self.allow(dst='10.0.2.2', sport=80, dport=40000, flags=0x18))
        self.assertFalse(self.allow(dst='10.0.2.2', sport=80, dport=40000, flags=2))
        self.assertFalse(self.allow(dst='10.0.2.2', sport=80, dport=40001, flags=0x10))
        self.assertFalse(self.allow(dst='10.0.2.2', sport=5901, dport=40000, flags=0x10))

    def test_offline_still_allows_forward_replies(self):
        self.p.mode = 'offline'
        self.assertFalse(self.allow())
        self.assertFalse(self.allow(dst='10.0.2.3', dport=53, proto=17))
        self.p.allow(packet(src='10.0.2.2', dst='10.0.2.15', sport=40000, dport=5901), 'from-passt')
        self.assertTrue(self.allow(dst='10.0.2.2', sport=5901, dport=40000, flags=0x12))

    def test_explicit_allow_cidr_cannot_override_host_block(self):
        self.p.allowed = [ipaddress.IPv4Network('10.1.0.0/16')]
        self.assertTrue(self.allow(dst='10.1.2.3'))
        self.assertFalse(self.allow(dst='10.1.1.79'))

    def test_malformed_fragments_spoofing_and_ipv6(self):
        self.assertFalse(self.allow(src='10.0.2.16'))
        frame = bytearray(packet())
        for fragment in (0x2000, 0x0001):
            frame[20:22] = struct.pack('!H', fragment)
            self.assertFalse(self.p.allow(frame, 'to-passt'))
        self.assertFalse(self.p.allow(packet()[:40], 'to-passt'))
        self.assertFalse(self.p.allow(b'\0' * 12 + b'\x86\xdd' + b'\0' * 80, 'to-passt'))
        self.assertFalse(self.p.allow(b'\0' * 12 + b'\x81\x00' + packet()[12:], 'to-passt'))


if __name__ == '__main__':
    unittest.main()

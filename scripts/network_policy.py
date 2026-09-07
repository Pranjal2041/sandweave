"""Outside-guest IPv4 egress policy for the lab's Ethernet transport."""
import collections
import ipaddress
import struct
import threading
import time


class NetworkPolicy:
    def __init__(self, config):
        self.mode = config['mode']
        if self.mode not in ('internet', 'offline'):
            raise ValueError('network mode must be internet or offline')
        self.guest = ipaddress.IPv4Address(config['guest']).packed
        self.gateway = ipaddress.IPv4Address(config['gateway']).packed
        self.dns = ipaddress.IPv4Address(config['dns']).packed
        self.hosts = {ipaddress.IPv4Address(a).packed for a in config['host_addresses']}
        self.ports = set(config['forwarded_tcp_ports'])
        self.allowed = [ipaddress.IPv4Network(n) for n in config.get('allow_cidrs', [])]
        self.flows = collections.OrderedDict()
        self.counts = collections.Counter()
        self.lock = threading.Lock()

    @staticmethod
    def ipv4(frame):
        if len(frame) < 34 or frame[12:14] != b'\x08\x00' or frame[14] != 0x45:
            return None
        size, fragment = struct.unpack_from('!HH', frame, 16)[0], struct.unpack_from('!H', frame, 20)[0]
        if size < 20 or len(frame) < 14 + size or fragment & 0x3fff:
            return None
        src, dst, proto = frame[26:30], frame[30:34], frame[23]
        payload = frame[34:14 + size]
        if proto == 6:
            if len(payload) < 20 or payload[12] >> 4 < 5 or (payload[12] >> 4) * 4 > len(payload):
                return None
        elif proto == 17:
            if len(payload) < 8 or not 8 <= struct.unpack_from('!H', payload, 4)[0] <= len(payload):
                return None
        elif proto == 1:
            if len(payload) < 8:
                return None
        else:
            return None
        return src, dst, proto, payload

    def allow(self, frame, direction):
        with self.lock:
            decision, reason = self._allow(frame, direction)
            self.counts[('allow_' if decision else 'deny_') + reason] += 1
            return decision

    def _allow(self, frame, direction):
        now = time.monotonic()
        while self.flows and next(iter(self.flows.values())) < now:
            self.flows.popitem(last=False)
        packet = self.ipv4(frame)
        if direction == 'from-passt':
            if packet:
                src, dst, proto, payload = packet
                if src == self.gateway and dst == self.guest and proto == 6:
                    sport, dport = struct.unpack_from('!HH', payload)
                    if dport in self.ports and payload[13] & 0x12 == 0x02:
                        key = (dport, sport)
                        if key not in self.flows and len(self.flows) >= 8192:
                            return False, 'flow_limit'
                        self.flows[key] = now + 3600
                        self.flows.move_to_end(key)
            return True, 'inbound'
        if len(frame) >= 42 and frame[12:14] == b'\x08\x06':
            # Only resolve the configured gateway/DNS. ARP never opens a host socket.
            arp = frame[14:42]
            valid = (arp[:6] == b'\x00\x01\x08\x00\x06\x04' and
                     arp[6:8] in (b'\x00\x01', b'\x00\x02') and
                     arp[14:18] in (self.guest, b'\0' * 4) and
                     arp[24:28] in (self.gateway, self.dns, self.guest))
            return valid, 'arp'
        if packet is None:
            return False, 'unsupported_or_malformed'
        src, dst, proto, payload = packet
        if src != self.guest:
            return False, 'source_spoof'
        if proto in (6, 17):
            sport, dport = struct.unpack_from('!HH', payload)
            if proto == 6 and dst == self.gateway:
                key = (sport, dport)
                # Guest responses to explicit host forwards are allowed. A guest
                # SYN without ACK can never create a new host connection here.
                if key in self.flows and payload[13] & 0x12 != 0x02:
                    self.flows[key] = now + 3600
                    self.flows.move_to_end(key)
                    return True, 'forward_reply'
            if self.mode == 'internet' and dst == self.dns and dport == 53:
                return True, 'dns'
        if self.mode == 'offline':
            return False, 'offline'
        addr = ipaddress.IPv4Address(dst)
        if dst in self.hosts or dst in (self.gateway, self.dns, self.guest) or addr.is_loopback or addr.is_link_local:
            return False, 'host'
        if not addr.is_global and not any(addr in network for network in self.allowed):
            return False, 'nonpublic'
        if addr.is_multicast or addr.is_unspecified or addr.is_reserved:
            return False, 'special'
        if proto == 1 and payload[:2] != b'\x08\x00':
            return False, 'icmp_type'
        return True, 'egress'

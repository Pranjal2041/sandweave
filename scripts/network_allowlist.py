"""IP admission derived from explicit networks and trusted DNS answers.

Hostname rules grant access to their resolved addresses, not HTTP virtual
hosts. Resolution and bounded DNS caching stay outside the guest's control.
"""
from collections import OrderedDict
import ipaddress
import re
import socket
import struct
import threading
import time


def entries(values):
    networks, names = [], []
    for value in values:
        value = value.strip().lower().rstrip('.')
        try:
            network = ipaddress.ip_network(value)
        except ValueError:
            hostname = value[2:] if value.startswith('*.') else value
            if not hostname or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                       for label in hostname.split('.')):
                raise ValueError('invalid allowlist hostname: ' + value)
            names.append(value)
        else:
            if network.version != 4:
                raise ValueError('the current sandbox network supports IPv4 allowlists')
            networks.append(network)
    return networks, names


def dns_name(data, offset):
    labels, following, seen = [], None, set()
    while True:
        if offset in seen or offset >= len(data) or len(seen) >= 128:
            raise ValueError('invalid DNS name')
        seen.add(offset)
        size = data[offset]
        offset += 1
        if size & 0xc0 == 0xc0:
            if offset >= len(data):
                raise ValueError('truncated DNS pointer')
            following = following or offset + 1
            offset = ((size & 63) << 8) | data[offset]
        elif size & 0xc0 or offset + size > len(data):
            raise ValueError('invalid DNS label')
        elif size:
            labels.append(data[offset:offset + size].decode('ascii').lower())
            offset += size
        else:
            return '.'.join(labels), following or offset


class Allowlist:
    def __init__(self, values, *, resolve=True):
        self.networks, self.names = entries(values)
        self.answers, self.queries = OrderedDict(), OrderedDict()
        self.streams = OrderedDict()
        self.lock = threading.Lock()
        if resolve and self.names:
            # Seed exact names for clients retaining a DNS cache across a phase
            # change. Guest lookups refresh entries without blocking forwarding.
            threading.Thread(target=self._resolve, daemon=True, name='sandweave-allowlist-dns').start()

    def matches(self, name):
        return any(name == rule or (rule.startswith('*.') and name.endswith(rule[1:])) for rule in self.names)

    def _resolve(self):
        for name in self.names:
            if name.startswith('*.'):
                continue
            try:
                addresses = {item[4][0] for item in socket.getaddrinfo(name, 0, socket.AF_INET, socket.SOCK_STREAM)}
            except OSError:
                continue
            with self.lock:
                for address in addresses:
                    self._remember(ipaddress.IPv4Address(address).packed, 60)

    def _remember(self, address, ttl):
        self.answers[address] = max(self.answers.get(address, 0), time.monotonic() + min(ttl, 86400))
        self.answers.move_to_end(address)
        while len(self.answers) > 8192:
            self.answers.popitem(last=False)

    def allows(self, packed):
        address = ipaddress.IPv4Address(packed)
        if any(address in network for network in self.networks):
            return True
        with self.lock:
            return self.answers.get(packed, 0) > time.monotonic()

    def observe_tcp(self, payload, port, *, response):
        """Reassemble bounded DNS-over-TCP messages, including retransmissions."""
        sequence = struct.unpack_from('!I', payload, 4)[0]
        flags = payload[13]
        data = payload[(payload[12] >> 4) * 4:]
        key, messages = (port, response), []
        with self.lock:
            if flags & 4:
                self.streams.pop(key, None)
                return
            state = self.streams.get(key)
            if flags & 2 or state is None or state['expires'] < time.monotonic():
                state = {'next': (sequence + bool(flags & 2)) & 0xffffffff,
                         'buffer': bytearray(), 'pending': {}, 'expires': time.monotonic() + 60}
                self.streams[key] = state
            self.streams.move_to_end(key)
            while len(self.streams) > 512:
                self.streams.popitem(last=False)
            if flags & 2:
                sequence = (sequence + 1) & 0xffffffff
            if data:
                state['pending'][sequence] = data
            if sum(map(len, state['pending'].values())) + len(state['buffer']) > 131072:
                self.streams.pop(key, None)
                return
            while state['pending']:
                ready = None
                for offset, part in state['pending'].items():
                    delta = (offset - state['next'] + 2**31) % 2**32 - 2**31
                    if delta <= 0:
                        ready = offset, part[-delta:] if delta < 0 else part
                        break
                if ready is None:
                    break
                offset, part = ready
                del state['pending'][offset]
                state['buffer'].extend(part)
                state['next'] = (state['next'] + len(part)) & 0xffffffff
            while len(state['buffer']) >= 2:
                size = struct.unpack_from('!H', state['buffer'])[0]
                if len(state['buffer']) < size + 2:
                    break
                messages.append(bytes(state['buffer'][2:2 + size]))
                del state['buffer'][:2 + size]
            if flags & 1:
                self.streams.pop(key, None)
        for message in messages:
            self.observe(message, port, response=response)

    def observe(self, data, port, *, response):
        """Caller admits only UDP DNS with the configured trusted resolver."""
        try:
            identity, flags, questions, count, _, _ = struct.unpack_from('!6H', data)
            if questions != 1 or bool(flags & 0x8000) != response or flags & 0x780f or count > 512:
                return
            name, offset = dns_name(data, 12)
            kind, klass = struct.unpack_from('!HH', data, offset)
            offset += 4
            key = (port, identity, name, kind)
            if klass != 1 or kind not in (1, 255) or not self.matches(name):
                return
            with self.lock:
                if not response:
                    self.queries[key] = time.monotonic() + 30
                    self.queries.move_to_end(key)
                    while len(self.queries) > 4096:
                        self.queries.popitem(last=False)
                    return
                if self.queries.pop(key, 0) < time.monotonic() or flags & 0x0200:
                    return
                records = []
                for _ in range(count):
                    owner, offset = dns_name(data, offset)
                    kind, klass, ttl, size = struct.unpack_from('!HHIH', data, offset)
                    offset += 10
                    if offset + size > len(data):
                        raise ValueError('truncated DNS record')
                    if klass == 1 and kind == 5:
                        target, _ = dns_name(data, offset)
                        records.append((owner, kind, ttl, target))
                    elif klass == 1 and kind == 1 and size == 4:
                        records.append((owner, kind, ttl, data[offset:offset + size]))
                    offset += size
                aliases = {name: 86400}
                for _ in range(len(records)):
                    changed = False
                    for owner, kind, ttl, target in records:
                        if owner in aliases and kind == 5 and target not in aliases:
                            aliases[target] = min(ttl, aliases[owner])
                            changed = True
                    if not changed:
                        break
                for owner, kind, ttl, target in records:
                    if owner in aliases and kind == 1:
                        self._remember(target, min(ttl, aliases[owner]))
        except (ValueError, UnicodeError, struct.error):
            return

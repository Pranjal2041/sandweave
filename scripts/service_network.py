"""Private IPv4 routing between the native sandboxes in a service group.

Every sandbox retains its own kernel, filesystem and normal passt egress.
Service traffic crosses a worker-local datagram socket, never a host IP port.
The worker registers membership before launching guests; guests cannot join
another group or choose their source identity.
"""
import ipaddress
from pathlib import Path
import socket
import struct
import threading

GUEST = ipaddress.IPv4Address('10.0.2.15').packed
DNS = ipaddress.IPv4Address('10.0.2.3').packed
GUEST_MAC = bytes.fromhex('020000000015')
GATEWAY_MAC = bytes.fromhex('020000000002')
MAX_FRAME = 9014


def checksum(data):
    if len(data) & 1:
        data += b'\0'
    total = sum(struct.unpack('!' + 'H' * (len(data) // 2), data))
    while total >> 16:
        total = (total & 0xffff) + (total >> 16)
    return (~total) & 0xffff


def route(frame, source, destination):
    """Translate only addresses; preserve ports, TCP state and fragment offsets."""
    if len(frame) < 34 or frame[12:14] != b'\x08\x00' or frame[14] >> 4 != 4:
        return None
    header = (frame[14] & 15) * 4
    length = struct.unpack_from('!H', frame, 16)[0]
    if header < 20 or length < header or len(frame) < 14 + length or frame[26:30] != GUEST:
        return None
    packet = bytearray(frame[:14 + length])
    old = packet[26:34]
    packet[:12] = GUEST_MAC + GATEWAY_MAC
    packet[26:34] = source + destination
    packet[24:26] = b'\0\0'
    struct.pack_into('!H', packet, 24, checksum(packet[14:14 + header]))
    fragment = struct.unpack_from('!H', packet, 20)[0]
    protocol = packet[23]
    # The transport checksum occurs only in the initial fragment. Updating its
    # pseudoheader incrementally also supports fragmented payloads.
    if not fragment & 0x1fff and protocol in (6, 17):
        offset = 14 + header + (16 if protocol == 6 else 6)
        if offset + 2 > len(packet):
            return None
        current = struct.unpack_from('!H', packet, offset)[0]
        if current or protocol == 6:
            total = (~current) & 0xffff
            total += sum((~word) & 0xffff for word in struct.unpack('!4H', old))
            total += sum(struct.unpack('!4H', source + destination))
            while total >> 16:
                total = (total & 0xffff) + (total >> 16)
            value = (~total) & 0xffff
            struct.pack_into('!H', packet, offset, value or (0xffff if protocol == 17 else 0))
    return bytes(packet)


def dns_reply(frame, names):
    """Answer service names on the ordinary guest DNS address (UDP A/AAAA)."""
    if (len(frame) < 54 or frame[12:14] != b'\x08\x00' or frame[14] != 0x45
            or frame[23] != 17 or frame[26:30] != GUEST
            or frame[30:34] != DNS or struct.unpack_from('!H', frame, 20)[0] & 0x3fff
            or struct.unpack_from('!H', frame, 36)[0] != 53):
        return None
    length = struct.unpack_from('!H', frame, 38)[0]
    if length < 20 or 34 + length > len(frame) or struct.unpack_from('!H', frame, 16)[0] != 20 + length:
        return None
    query = frame[42:34 + length]
    if len(query) < 12 or query[2] & 0x80 or query[4:6] != b'\0\1':
        return None
    labels, end = [], 12
    while end < len(query) and query[end]:
        size = query[end]
        if size > 63 or end + 1 + size >= len(query):
            return None
        try:
            labels.append(query[end + 1:end + size + 1].decode('ascii').lower())
        except UnicodeError:
            return None
        end += size + 1
    end += 1
    if end + 4 > len(query):
        return None
    name = '.'.join(labels)
    address = names.get(name)
    if address is None:
        return None
    kind, cls = struct.unpack_from('!HH', query, end)
    answer = (b'\xc0\x0c\0\1\0\1\0\0\0\0\0\4' + ipaddress.IPv4Address(address).packed
              if kind == 1 and cls == 1 else b'')
    dns = query[:2] + b'\x81\x80\0\1' + struct.pack('!H', bool(answer)) + b'\0\0\0\0'
    dns += query[12:end + 4] + answer
    udp = frame[36:38] + frame[34:36] + struct.pack('!HH', 8 + len(dns), 0) + dns
    ip = bytearray(frame[14:34])
    struct.pack_into('!H', ip, 2, 20 + len(udp))
    ip[12:20] = frame[30:34] + GUEST
    ip[10:12] = b'\0\0'
    struct.pack_into('!H', ip, 10, checksum(ip))
    return frame[6:12] + frame[:6] + b'\x08\x00' + bytes(ip) + udp


class Hub:
    """One bounded router per worker, shared by otherwise isolated groups."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The worker holds its process lock for its lifetime. Keeping this path
        # stable lets running relays resume routing after a worker restart.
        self.path = self.directory / 'mesh.sock'
        self.path.unlink(missing_ok=True)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.bind(str(self.path))
        self.socket.settimeout(.2)
        self.members, self.groups = {}, {}
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, name='sandweave-service-network', daemon=True)
        self.thread.start()

    def register(self, identity, members):
        with self.lock:
            if identity in self.groups:
                raise ValueError('service network is already registered')
            routes = {}
            for entry in members:
                address = ipaddress.IPv4Address(entry['address']).packed
                path = str(entry['socket'])
                if path in self.members or address in routes:
                    raise ValueError('duplicate service network member')
                routes[address] = (path, frozenset(entry['networks']))
            self.groups[identity] = routes
            for address, (path, networks) in routes.items():
                self.members[path] = (identity, address, networks)

    def unregister(self, identity):
        with self.lock:
            for path, _ in self.groups.pop(identity, {}).values():
                self.members.pop(path, None)

    def run(self):
        while not self.stopping.is_set():
            try:
                frame, _, flags, origin = self.socket.recvmsg(MAX_FRAME)
                if flags & socket.MSG_TRUNC or len(frame) < 34:
                    continue
                with self.lock:
                    member = self.members.get(origin)
                    if member is None:
                        continue
                    group, address, networks = member
                    target = self.groups[group].get(frame[30:34])
                    if target is None or not networks.intersection(target[1]):
                        continue
                translated = route(frame, address, GUEST)
                if translated is not None:
                    # A stopped or backpressured receiver drops packets exactly
                    # as a network would; unrelated groups keep forwarding.
                    self.socket.sendto(translated, socket.MSG_DONTWAIT, target[0])
            except socket.timeout:
                continue
            except OSError:
                if self.stopping.is_set():
                    break

    def close(self):
        if self.stopping.is_set():
            return
        self.stopping.set()
        self.thread.join()
        self.socket.close()
        self.path.unlink(missing_ok=True)


class Peer:
    def __init__(self, config, packet):
        self.config, self.packet = config, packet
        self.path = config['socket']
        self.addresses = {ipaddress.IPv4Address(value).packed for value in config['peers'].values()}
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.socket.bind(self.path)
        self.socket.settimeout(.2)
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, name='sandweave-service-peer', daemon=True)
        self.thread.start()

    def send(self, frame):
        reply = dns_reply(frame, self.config['peers'])
        if reply is not None:
            try:
                self.packet.send(reply, socket.MSG_DONTWAIT)
            except BlockingIOError:
                pass
            return True
        if len(frame) < 34 or frame[12:14] != b'\x08\x00' or frame[30:34] not in self.addresses:
            return False
        try:
            self.socket.sendto(frame, socket.MSG_DONTWAIT, self.config['hub'])
        except OSError:
            # A worker restart may briefly remove the router. Do not take down
            # the guest's public network while the service router reconnects.
            pass
        return True

    def run(self):
        while not self.stopping.is_set():
            try:
                frame, origin = self.socket.recvfrom(MAX_FRAME)
                if frame and origin == self.config['hub']:
                    self.packet.send(frame, socket.MSG_DONTWAIT)
            except BlockingIOError:
                continue
            except socket.timeout:
                continue
            except OSError:
                break

    def close(self):
        self.stopping.set()
        self.thread.join()
        self.socket.close()
        Path(self.path).unlink(missing_ok=True)

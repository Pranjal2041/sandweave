#!/usr/bin/env python3
"""Check service primitives inside a disposable OSWorld sandbox."""
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import socket
import struct
import subprocess


def palette():
    fd = os.open('/dev/tty10', os.O_RDWR | os.O_NOCTTY)
    original = bytearray(48)
    try:
        keyboard = bytearray(1)
        fcntl.ioctl(fd, 0x4b33, keyboard)
        assert keyboard == b'\x02', keyboard
        fcntl.ioctl(fd, 0x4b70, original)
        changed = bytes((i * 7) % 256 for i in range(48))
        fcntl.ioctl(fd, 0x4b71, changed)
        readback = bytearray(48)
        with open('/dev/tty11', 'rb', buffering=0) as other:
            fcntl.ioctl(other, 0x4b70, readback)
        assert readback == changed
        child = os.fork()
        if child == 0:
            os.setgid(1000)
            os.setuid(1000)
            try:
                fcntl.ioctl(fd, 0x4b71, original)
            except OSError as error:
                os._exit(0 if error.errno == errno.EPERM else 2)
            os._exit(1)
        assert os.waitpid(child, 0)[1] == 0, 'unprivileged palette write was allowed'
    finally:
        fcntl.ioctl(fd, 0x4b71, original)
        os.close(fd)
    return 'passed'


def sysctls():
    result = {}
    for key in ('kernel/pid_max', 'vm/mmap_min_addr'):
        path = Path('/proc/sys') / key
        current = int(path.read_text())
        path.write_text(str(current) + '\n')
        for unsupported in ('garbage', str(current + 1), '-1'):
            try:
                path.write_text(unsupported)
            except OSError as error:
                assert error.errno == errno.EINVAL, error
            else:
                raise AssertionError('unsupported sysctl write succeeded: ' + key)
        assert int(path.read_text()) == current
        result[key] = current
    assert result['kernel/pid_max'] == 4194304, result
    # Verify the advertised lower mapping boundary with a real mmap syscall.
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mmap.restype = ctypes.c_void_p
    floor = result['vm/mmap_min_addr']
    if floor:
        mapped = libc.mmap(ctypes.c_void_p(floor - 4096), ctypes.c_size_t(4096),
                          3, 0x32, -1, 0)  # private anonymous MAP_FIXED
        assert mapped == ctypes.c_void_p(-1).value, mapped
    return result


def address_events():
    events = []
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as listener:
        listener.bind((0, 0x111))  # link, IPv4 address, IPv6 address
        assert listener.getsockname()[1] == 0x111
        listener.settimeout(3)
        for family, address in ((socket.AF_INET, '192.0.2.123/32'),
                                (socket.AF_INET6, '2001:db8::123/128')):
            packed = socket.inet_pton(family, address.split('/')[0])
            def receive(expected):
                while True:
                    packet = listener.recv(65536)
                    while len(packet) >= 16:
                        size, kind, flags, sequence, sender = struct.unpack_from('IHHII', packet)
                        assert 16 <= size <= len(packet)
                        payload, packet = packet[16:size], packet[(size + 3) & ~3:]
                        if kind == expected and len(payload) >= 8 and payload[0] == family and packed in payload:
                            assert sender == sequence == 0
                            return
            try:
                subprocess.run(['ip', 'addr', 'add', address, 'dev', 'lo'], check=True)
                receive(20)
            finally:
                subprocess.run(['ip', 'addr', 'del', address, 'dev', 'lo'], check=True)
            receive(21)
            events.append({'family': family, 'added': True, 'removed': True})
    return events


def interface_removal():
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as listener:
        listener.bind((0, 0x10))
        listener.settimeout(3)
        subprocess.run(['ip', 'link', 'add', 'swprobe', 'type', 'bridge'], check=True)
        try:
            subprocess.run(['ip', 'addr', 'add', '192.0.2.125/32', 'dev', 'swprobe'], check=True)
            added = listener.recv(65536)
            assert struct.unpack_from('H', added, 4)[0] == 20
        finally:
            subprocess.run(['ip', 'link', 'del', 'swprobe'], check=True)
        removed = listener.recv(65536)
        assert struct.unpack_from('H', removed, 4)[0] == 21
        assert socket.inet_aton('192.0.2.125') in removed
    return 'passed'


def namespace_isolation():
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as listener:
        listener.bind((0, 0x110))
        listener.settimeout(0.25)
        subprocess.run(['unshare', '--net', 'sh', '-ec',
                        'ip addr add 192.0.2.124/32 dev lo; ip addr del 192.0.2.124/32 dev lo'], check=True)
        try:
            leaked = listener.recv(65536)
        except socket.timeout:
            return 'passed'
        raise AssertionError('address event escaped its network namespace: ' + leaked.hex())


def mdns():
    # A legacy unicast mDNS query exercises Avahi's UDP listener and real DNS
    # response, independently of the daemon's D-Bus registry/cache.
    name = socket.gethostname().split('.')[0] + '.local'
    labels = b''.join(bytes([len(part)]) + part.encode() for part in name.split('.')) + b'\0'
    query = struct.pack('!6H', 12001, 0, 1, 0, 0, 0) + labels + struct.pack('!HH', 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.bind(('127.0.0.1', 0))
        client.settimeout(5)
        client.sendto(query, ('127.0.0.1', 5353))
        response, sender = client.recvfrom(65536)
    identity, flags, questions, answers, _, _ = struct.unpack_from('!6H', response)
    assert identity == 12001 and flags & 0x8000 and answers > 0, response.hex()
    def skip_name(offset):
        while response[offset]:
            if response[offset] & 0xc0 == 0xc0:
                return offset + 2
            offset += 1 + response[offset]
        return offset + 1
    offset = 12
    for _ in range(questions):
        offset = skip_name(offset) + 4
    addresses = []
    for _ in range(answers):
        offset = skip_name(offset)
        kind, _, _, size = struct.unpack_from('!HHIH', response, offset)
        offset += 10
        if kind == 1 and size == 4:
            addresses.append(socket.inet_ntop(socket.AF_INET, response[offset:offset + size]))
        offset += size
    assert '127.0.0.1' in addresses, addresses
    return {'name': name, 'addresses': addresses, 'responder': list(sender)}


def services():
    result = {}
    for unit in ('avahi-daemon', 'setvtrgb', 'systemd-sysctl'):
        text = subprocess.check_output(['systemctl', 'show', unit, '--property=ActiveState,SubState,Result,ExecMainStatus'], text=True)
        result[unit] = dict(line.split('=', 1) for line in text.splitlines())
    return result


if __name__ == '__main__':
    checks = {'services': services()}
    for name, function in (('palette', palette), ('sysctls', sysctls),
                           ('address_events', address_events), ('namespace_isolation', namespace_isolation),
                           ('interface_removal', interface_removal), ('mdns', mdns)):
        try:
            checks[name] = function()
        except Exception as error:
            checks[name] = {'error': repr(error)}
    print(json.dumps(checks), flush=True)
    assert all(value.get('Result') == 'success' for value in checks['services'].values()), checks
    assert all(not isinstance(value, dict) or 'error' not in value for value in checks.values()), checks

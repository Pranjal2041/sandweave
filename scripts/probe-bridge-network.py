#!/usr/bin/env python3
"""Bridge TCP regression probe. Run only inside a disposable network namespace.

The same probe runs on Linux and in a Sandweave sandbox. It deliberately leaves
IP forwarding enabled. No Docker, downloaded image, or application is needed.
"""
import argparse
from collections import Counter
import concurrent.futures
import json
from pathlib import Path
import selectors
import socket
import struct
import subprocess
import sys
import time


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True).stdout


def serve(port, report):
    selector = selectors.DefaultSelector()
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('0.0.0.0', port))
    listener.listen(256)
    listener.setblocking(False)
    selector.register(listener, selectors.EVENT_READ, 'listen')
    capture = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
    capture.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16*1024*1024)
    capture.bind(('eth0', 0))
    capture.setblocking(False)
    selector.register(capture, selectors.EVENT_READ, 'capture')
    syns = Counter()
    ttl = Counter()
    macs = Counter()
    accepted = 0
    Path(report + '.ready').touch()
    while not Path(report + '.stop').exists():
        for key, _ in selector.select(.05):
            sock = key.fileobj
            if key.data == 'listen':
                conn, _ = sock.accept()
                accepted += 1
                conn.setblocking(False)
                selector.register(conn, selectors.EVENT_READ, 'echo')
            elif key.data == 'capture':
                for _ in range(256):
                    try:
                        packet = sock.recv(65535)
                    except BlockingIOError:
                        break
                    if (len(packet) >= 54 and packet[12:14] == b'\x08\x00'
                            and packet[23] == 6):
                        offset = 14 + (packet[14] & 15) * 4
                        if len(packet) >= offset+20 and packet[offset+13] & 0x12 == 2:
                            source, destination = struct.unpack('!HH', packet[offset:offset+4])
                            if destination == port:
                                syns[str(source)] += 1
                                ttl[str(packet[22])] += 1
                                macs[packet[6:12].hex(':')] += 1
            else:
                try:
                    data = sock.recv(100)
                    if data:
                        sock.sendall(data)
                except OSError:
                    pass
                finally:
                    selector.unregister(sock)
                    sock.close()
    try:
        packets, drops = struct.unpack('II', capture.getsockopt(263, 6, 8))  # SOL_PACKET, PACKET_STATISTICS
        if packets == 0 and syns:
            # Some runtimes expose the option but do not implement its counters.
            packets, drops = None, None
    except OSError:
        packets, drops = None, None
    Path(report).write_text(json.dumps({'accepted': accepted, 'syns': dict(syns),
                                      'ttl': dict(ttl), 'source_macs': dict(macs),
                                      'capture_packets': packets, 'capture_drops': drops}, indent=2))


def client(host, port, count, concurrency, start_port=30000):
    def one(index):
        source = start_port + index
        try:
            with socket.socket() as conn:
                conn.settimeout(.5)
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                conn.bind(('0.0.0.0', source))
                conn.connect((host, port))
                conn.sendall(b'bridge-echo')
                data = b''
                while len(data) < 11:
                    chunk = conn.recv(11-len(data))
                    if not chunk:
                        break
                    data += chunk
                if data != b'bridge-echo':
                    return {'port': source, 'error': repr(data)}
        except OSError as error:
            return {'port': source, 'error': str(error)}
        return None
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        errors = [r for r in executor.map(one, range(count)) if r is not None]
    print(json.dumps({'connections': count, 'concurrency': concurrency, 'errors': errors,
                      'seconds': time.monotonic()-started}), flush=True)


def probe(directory, count, concurrency):
    directory.mkdir(parents=True, exist_ok=True)
    script = str(Path(__file__).resolve())
    run('ip', 'link', 'add', 'brtest', 'type', 'bridge')
    run('ip', 'addr', 'add', '10.83.0.1/24', 'dev', 'brtest')
    run('ip', 'link', 'set', 'brtest', 'up')
    Path('/proc/sys/net/ipv4/ip_forward').write_text('1')
    for index, name in enumerate(('a', 'b'), 2):
        run('ip', 'netns', 'add', name)
        run('ip', 'link', 'add', 'v'+name, 'type', 'veth', 'peer', 'name', 'eth0', 'netns', name)
        run('ip', 'link', 'set', 'v'+name, 'master', 'brtest')
        run('ip', 'link', 'set', 'v'+name, 'up')
        run('ip', '-n', name, 'addr', 'add', f'10.83.0.{index}/24', 'dev', 'eth0')
        run('ip', '-n', name, 'link', 'set', 'eth0', 'up')
        run('ip', '-n', name, 'link', 'set', 'lo', 'up')
        run('ip', '-n', name, 'route', 'add', 'default', 'via', '10.83.0.1')
    capture = str(directory / 'capture.json')
    server = subprocess.Popen(['ip', 'netns', 'exec', 'b', sys.executable, script,
                               'server', '--report', capture])
    try:
        deadline = time.monotonic()+10
        while not Path(capture+'.ready').exists():
            assert server.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.02)
        result = json.loads(run('ip', 'netns', 'exec', 'a', sys.executable, script,
                                'client', '--count', str(count), '--concurrency', str(concurrency)))
    finally:
        Path(capture+'.stop').touch()
        server.wait(timeout=10)
    result['capture'] = json.loads(Path(capture).read_text())
    result['forwarding'] = Path('/proc/sys/net/ipv4/ip_forward').read_text().strip()
    (directory/'result.json').write_text(json.dumps(result, indent=2)+'\n')
    summary = {**result, 'capture': {k:v for k,v in result['capture'].items() if k != 'syns'}}
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('probe', 'client', 'server'))
    parser.add_argument('--report', default='/tmp/bridge-probe')
    parser.add_argument('--count', type=int, default=20000)
    parser.add_argument('--concurrency', type=int, default=1)
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--host', default='10.83.0.3')
    parser.add_argument('--start-port', type=int, default=30000)
    args = parser.parse_args()
    if args.mode == 'server':
        serve(args.port, args.report)
    elif args.mode == 'client':
        client(args.host, args.port, args.count, args.concurrency, args.start_port)
    else:
        probe(Path(args.report), args.count, args.concurrency)

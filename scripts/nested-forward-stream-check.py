#!/usr/bin/env python3
"""Verify byte streams through a sandbox's forwarded TCP port against a guest echo server."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import random
import socket
import time

from sandweave import Sandbox

ECHO = r'''
import socket, threading
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("0.0.0.0", PORT))
server.listen(128)
def serve(connection):
    with connection:
        while data := connection.recv(1 << 20):
            connection.sendall(data)
while True:
    threading.Thread(target=serve, args=(server.accept()[0],), daemon=True).start()
'''


def exchange(host_port, index, rounds, seed):
    generator = random.Random(seed * 1000003 + index)
    sent = received = 0
    with socket.create_connection(('127.0.0.1', host_port), timeout=60) as connection:
        for round_number in range(rounds):
            size = generator.choice((1, 7, 64, 512, 1460, 4096, 65536, 262144, generator.randrange(1, 1 << 20)))
            message = generator.randbytes(size)
            connection.sendall(message)
            sent += size
            echoed = bytearray()
            while len(echoed) < size:
                chunk = connection.recv(min(1 << 20, size - len(echoed)))
                if not chunk:
                    return {'index': index, 'round': round_number, 'error': 'connection closed'}
                echoed += chunk
            received += len(echoed)
            if echoed != message:
                first = next(i for i, (a, b) in enumerate(zip(echoed, message)) if a != b)
                return {'index': index, 'round': round_number, 'size': size, 'first_mismatch': first,
                        'expected_sha256': hashlib.sha256(message).hexdigest(),
                        'actual_sha256': hashlib.sha256(echoed).hexdigest()}
    return {'index': index, 'sent': sent, 'received': received}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sandbox')
    parser.add_argument('--ports-json', required=True, type=Path)
    parser.add_argument('--guest-port', type=int, default=8080)
    parser.add_argument('--connections', type=int, default=16)
    parser.add_argument('--rounds', type=int, default=400)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    host_port = json.loads(args.ports_json.read_text())[str(args.guest_port)]
    with Sandbox.connect(args.sandbox) as sandbox:
        sandbox.files.write_text('/tmp/forward-echo.py', ECHO.replace('PORT', str(args.guest_port)))
        server = sandbox.exec(argv=['python3', '/tmp/forward-echo.py'], user='root')
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    socket.create_connection(('127.0.0.1', host_port), timeout=5).close()
                    probe = exchange(host_port, -1, 1, 0)
                    if 'sent' in probe:
                        break
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError('guest echo server did not become reachable')
                time.sleep(.5)
            started = time.monotonic()
            with ThreadPoolExecutor(args.connections) as pool:
                results = list(pool.map(lambda i: exchange(host_port, i, args.rounds, 1),
                                        range(args.connections)))
            seconds = time.monotonic() - started
        finally:
            server.terminate()
    failures = [result for result in results if 'sent' not in result]
    report = {'sandbox': args.sandbox, 'host_port': host_port, 'guest_port': args.guest_port,
              'connections': args.connections, 'rounds': args.rounds, 'seconds': seconds,
              'bytes_each_way': sum(result.get('sent', 0) for result in results),
              'failures': failures}
    (args.output / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: report[key] for key in ('seconds', 'bytes_each_way')} | {'failed': len(failures)}))
    for failure in failures:
        print(json.dumps(failure))
    raise SystemExit(1 if failures else 0)


if __name__ == '__main__':
    main()

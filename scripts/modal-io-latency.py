#!/usr/bin/env python3
"""Measure the Modal relay round trip for a runtime="modal" VR sandbox, by segment.

Run on the worker host. Pings guest_io.py from the worker through Modal's TLS
tunnel and, inside the guest, over loopback; times SDK VR actions; records the
worker's and the sandbox's Modal regions; and repeats the pings with the
guest's audio capture (parec) stopped, to separate network distance from
relay processing.

    scripts/modal-io-latency.py --output DIR [--gpu L40S]
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import time

from sandweave import GPU, Sandbox

GUEST_PING = r'''
import socket, struct, sys, time
sys.path.insert(0, "/opt/sandweave")
from sandweave_wire import decode, encode
LENGTH = struct.Struct("<I")
token = open("/run/sandweave/agent.token").read().strip()
connection = socket.create_connection(("127.0.0.1", 23801))
connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
def send(value):
    payload = encode(value)
    connection.sendall(LENGTH.pack(len(payload)) + payload)
def receive():
    data = b""
    while len(data) < 4:
        data += connection.recv(4 - len(data))
    size, body = LENGTH.unpack(data)[0], b""
    while len(body) < size:
        body += connection.recv(size - len(body))
    return decode(body)
send({"token": token})
assert receive()["ready"]
samples = []
for identity in range(50):
    sent = time.monotonic_ns()
    send({"id": identity, "op": "ping", "params": {}})
    receive()
    samples.append((time.monotonic_ns() - sent) / 1e6)
print(" ".join(f"{s:.3f}" for s in samples))
'''


def summary(samples):
    return {'count': len(samples), 'min_ms': min(samples), 'median_ms': statistics.median(samples),
            'max_ms': max(samples)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--gpu', default='L40S')
    parser.add_argument('--startup-timeout', type=float, default=3600)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    from sandweave.sandbox.runtimes.modal.io_client import IOConnection
    from sandweave.sandbox.wire import decode
    result = {'worker_region': os.environ.get('MODAL_REGION'), 'worker_cloud': os.environ.get('MODAL_CLOUD_PROVIDER')}
    with Sandbox(template='vr/opensaber', runtime='modal', gpu=GPU(model=args.gpu),
                 startup_timeout=args.startup_timeout) as sandbox:
        environment = sandbox.run("tr '\\0' '\\n' < /proc/1/environ | grep '^MODAL_' || true", user='root').stdout
        result['sandbox_modal_environment'] = dict(line.split('=', 1) for line in environment.split() if '=' in line
                                                   and line.split('=')[0] in ('MODAL_REGION', 'MODAL_CLOUD_PROVIDER'))
        record = next(Path(os.environ['SANDWEAVE_HOME']).rglob(sandbox.id + '.bin'))
        metadata = decode(record.read_bytes())['agent']

        def tunnel():
            connection = IOConnection(metadata['io']['host'], metadata['io']['port'], metadata['token'])
            samples = []
            for _ in range(50):
                sent = time.monotonic_ns()
                connection.call('ping')
                samples.append((time.monotonic_ns() - sent) / 1e6)
            connection.close()
            return summary(samples)

        def loopback():
            output = sandbox.run(argv=['python3', '-c', GUEST_PING], user='root', check=True).stdout
            return summary([float(value) for value in output.split()])

        def actions():
            samples = []
            for index in range(50):
                sent = time.monotonic_ns()
                sandbox.vr.action({'right': {'position': [.2, 1.3 + index * .001, -.5]}})
                samples.append((time.monotonic_ns() - sent) / 1e6)
            return summary(samples)

        result['with_audio_capture'] = {'tunnel_ping': tunnel(), 'guest_loopback_ping': loopback(),
                                        'sdk_vr_action': actions()}
        result['guest_threads'] = sandbox.run('top -b -n 1 -H -w 200 | head -25', user='root').stdout
        sandbox.run('pkill -STOP -x parec', user='root', check=True)
        try:
            result['audio_capture_stopped'] = {'tunnel_ping': tunnel(), 'guest_loopback_ping': loopback(),
                                               'sdk_vr_action': actions()}
        finally:
            sandbox.run('pkill -CONT -x parec', user='root', check=True)
    (args.output / 'latency.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()

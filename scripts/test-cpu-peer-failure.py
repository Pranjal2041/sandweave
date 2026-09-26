#!/usr/bin/env python3
"""Inject a peer's control-socket failure and verify its CPU pool stays alive."""
import argparse
import json
import os
import signal
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
from environment import EnvironmentManager

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('observer')
a = p.parse_args()
lab = Path(__file__).resolve().parent.parent
local = Path((lab / 'runs/local-path.txt').read_text().strip())
observer_reg = next((local / 'gvisor/cpu-brokers').glob('*/job-' + a.observer + '.json'))
directory = observer_reg.parent
cpus = json.loads(observer_reg.read_text())['cpus']
name = 'peer-failure-' + str(time.time_ns())
code = 'import time\nend=time.monotonic()+45\nx=1\nwhile time.monotonic()<end:\n for i in range(20000):x=(x*1664525+1013904223)&0xffffffff\n'
subprocess.run([sys.executable, str(lab / 'scripts/run-gvisor.py'), '--detach', '--cpus', ','.join(map(str, cpus)),
                '--memory-mib', '256', '--cpu-policy', 'quota', '--cpu-quota', '.1', name, '--', 'python3', '-c', code], check=True)
logs = lab / 'runs/gvisor' / name
registration = 'job-' + name + '.json'
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    status = json.loads((directory / 'status.json').read_text())
    if status['jobs'].get(registration, {}).get('stops', 0) > 1:
        break
    time.sleep(.1)
else:
    raise RuntimeError('peer did not enter controlled execution')
broker = status['pid']
manager = EnvironmentManager(lab)
sentry = manager.status(name)['sentry']['pid']
os.kill(sentry, signal.SIGSTOP)
# The peer cannot answer any control RPC while its host runtime is frozen.
start = time.monotonic()
try:
    deadline = start + 12
    while time.monotonic() < deadline:
        if (logs / 'cpu-controller-degraded.txt').exists():
            break
        time.sleep(.1)
    else:
        raise RuntimeError('stalled peer was not marked degraded')
    os.kill(sentry, signal.SIGCONT)
    assert manager._run([*manager._command(name), 'exec', name, '/bin/echo', 'survived']).strip() == 'survived'
    current = json.loads((directory / 'status.json').read_text())
    assert current['pid'] == broker and time.time() - current['time'] < 1, current
    assert 'job-' + a.observer + '.json' in current['jobs'], current
    observer_logs = lab / 'runs/gvisor' / a.observer
    assert not (observer_logs / 'exit-code.txt').exists()
    ports = json.loads((observer_logs / 'ports.json').read_text())
    with urllib.request.urlopen('http://127.0.0.1:' + str(ports['8000']), timeout=5) as r:
        probe = json.load(r)
    with socket.create_connection(('127.0.0.1', ports['5901']), timeout=5) as r:
        banner = r.recv(12).decode().strip()
    assert banner.startswith('RFB ')
    result = {'observer': a.observer, 'failed_peer': name, 'same_broker_survived': True,
              'peer_survived': not (logs / 'exit-code.txt').exists(),
              'peer_failure_marked': (logs / 'cpu-controller-degraded.txt').exists(),
              'observer_probe': probe, 'observer_vnc': banner, 'elapsed_seconds': time.monotonic() - start}
    assert result['peer_failure_marked'], result
    (observer_logs / 'peer-failure-acceptance.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
finally:
    try:
        os.kill(sentry, signal.SIGCONT)
    except ProcessLookupError:
        pass
    manager.stop(name, discard=True)

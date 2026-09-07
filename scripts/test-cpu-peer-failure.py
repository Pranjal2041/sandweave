#!/usr/bin/env python3
"""Inject a peer's control-socket failure and verify its CPU pool stays alive."""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

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
control = local / 'gvisor/state' / ('runsc-' + name + '.sock')
saved_control = control.with_suffix('.fault-injection-original')
control.rename(saved_control)
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as dead:
    dead.bind(str(control))
# A bound, closed socket makes the next scheduling RPC fail deterministically.
start = time.monotonic()
try:
    deadline = start + 12
    while time.monotonic() < deadline:
        if (logs / 'exit-code.txt').exists():
            break
        time.sleep(.1)
    else:
        raise RuntimeError('failed peer was not stopped')
    time.sleep(4)
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
              'peer_exit_code': (logs / 'exit-code.txt').read_text().strip(),
              'peer_failure_marked': (logs / 'cpu-controller-failed.txt').exists(),
              'observer_probe': probe, 'observer_vnc': banner, 'elapsed_seconds': time.monotonic() - start}
    assert result['peer_failure_marked'], result
    (observer_logs / 'peer-failure-acceptance.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
finally:
    saved_control.unlink(missing_ok=True)
    control.unlink(missing_ok=True)

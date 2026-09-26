#!/usr/bin/env python3
"""Kill a disposable environment's CPU broker and verify guest survival."""
import argparse
import json
import os
from pathlib import Path
import signal
import time
import cpu_broker
from environment import EnvironmentManager

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('name')
p.add_argument('--lab', type=Path, default=Path(__file__).resolve().parent.parent)
a = p.parse_args()
lab = a.lab.resolve()
local = Path((lab / 'runs/local-path.txt').read_text().strip())
logs = lab / 'runs/gvisor' / a.name
registrations = list((local / 'gvisor/cpu-brokers').glob('*/job-' + a.name + '.json'))
assert len(registrations) == 1, registrations
registration = registrations[0]
deadline = time.monotonic() + 40
while time.monotonic() < deadline:
    try:
        status = json.loads((registration.parent / 'status.json').read_text())
        job = status['jobs'][registration.name]
        if job['paused']:
            break
    except (OSError, ValueError, KeyError):
        pass
    time.sleep(.05)
else:
    raise RuntimeError('test guest did not enter a CPU pause')
assert set(status['jobs']) == {registration.name}, 'Refusing to disrupt another registered environment'
root = int((logs / 'launcher-pid.txt').read_text())
before = cpu_broker.process_table([root])[root]
os.kill(status['pid'], signal.SIGKILL)
start = time.monotonic()
while time.monotonic() - start < 12:
    if (logs / 'cpu-controller-degraded.txt').exists():
        break
    time.sleep(.1)
manager = EnvironmentManager(lab)
output = manager._run([*manager._command(a.name), 'exec', a.name, '/bin/echo', 'survived'])
current = cpu_broker.process_table([root]).get(root)
result = {'was_paused': True, 'controller_pid': status['pid'],
          'recovery_seconds': time.monotonic() - start,
          'degraded_marker': (logs / 'cpu-controller-degraded.txt').exists(),
          'launcher_survived': current is not None and current['start'] == before['start'],
          'command_output': output.strip(),
          'exit_code': (logs / 'exit-code.txt').read_text() if (logs / 'exit-code.txt').exists() else None}
(logs / 'failure-acceptance.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
assert result['launcher_survived'] and result['degraded_marker'] and output.strip() == 'survived', result

#!/usr/bin/env python3
"""Measure CPU sharing and per-thread nice under different guest process counts."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

lab = Path(__file__).resolve().parent.parent
stamp = str(time.time_ns())
results = []
broker = '--broker' in sys.argv
duration = 20 if '--long' in sys.argv else 8
local = Path((lab / 'runs/local-path.txt').read_text().strip())
program = '''import os,time,json
start=START
children=[]
readfd,writefd=os.pipe()
for i in range(WORKERS):
 p=os.fork()
 if p==0:
  while time.time()<start:time.sleep(.01)
  n=1;batches=0
  while time.time()<start+DURATION:
   for j in range(20000):n=(n*1664525+1013904223)&0xffffffff
   batches+=1
  os.write(writefd,(str(batches)+'\\n').encode())
  os._exit(0)
 children.append(p)
if WORKERS==0:time.sleep(max(0,start+DURATION-time.time()))
for p in children:os.waitpid(p,0)
os.close(writefd)
batches=sum(map(int,os.read(readfd,65536).split()))
print('CPU_RESULT '+json.dumps({'batches':batches,'workers':WORKERS}),flush=True)
time.sleep(3)
'''

def host_cpu(roots):
    processes = {}
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            data = path.read_text(); fields = data[data.rfind(')') + 2:].split()
            # Per-TGID user/system time plus reaped descendants. CLONE_VM
            # processes have separate CPU time even when they share memory.
            processes[int(path.parent.name)] = (int(fields[1]), sum(map(int, fields[11:15])), int(fields[16]))
        except (OSError, ValueError):
            pass
    excluded = set()
    for path in (local / 'gvisor/cpu-brokers').glob('*/status.json'):
        try: excluded.add(json.loads(path.read_text())['pid'])
        except (OSError, ValueError): pass
    answer = []
    for root in roots:
        members = {root}
        while True:
            extra = {pid for pid, value in processes.items() if value[0] in members} - members
            if not extra: break
            members.update(extra)
        members -= excluded
        answer.append({'seconds': sum(processes[p][1] for p in members if p in processes) / os.sysconf('SC_CLK_TCK'),
                       'nice_values': sorted({processes[p][2] for p in members if p in processes})})
    return answer
trials = [('equal-4-4', (4, 4), (0, 0)),
                              ('nice-4-4', (4, 4), (0, 5)),
                              ('equal-4-16', (4, 16), (0, 0)),
                              ('nice-4-16', (4, 16), (0, 5))]
if broker:
    trials = [('weighted-equal-4-4', (4, 4), (0, 0)),
              ('weighted-equal-4-16', (4, 16), (0, 0)),
              ('weighted-3-to-1-4-16', (4, 16), (0, 0)),
              ('quota-one-each-4-16', (4, 16), (0, 0))]
if '--idle' in sys.argv:
    trials = [('weighted-idle-borrow', (4, 0), (0, 0))]
if '--single' in sys.argv:
    trials = trials[:1]
for trial, workers, nice in trials:
    start = time.time() + 5
    names = []
    for side in range(2):
        name = f'cpu-{stamp}-{trial}-{side}'
        names.append(name)
        code = program.replace('START', repr(start)).replace('WORKERS', str(workers[side])).replace('DURATION', str(duration))
        extra = ['--cpu-policy', 'shared']
        if broker:
            extra = ['--cpu-policy', 'quota' if trial.startswith('quota') else 'weighted',
                     '--cpu-weight', str(300 if '3-to-1' in trial and side == 0 else 100)]
            if trial.startswith('quota'):
                extra += ['--cpu-quota', '1']
        subprocess.run([sys.executable, str(lab / 'scripts/run-gvisor.py'), '--detach', '--cpus', '48-51', *extra,
                        '--guest-cpus', '4', '--memory-mib', '512', '--host-nice', str(nice[side]),
                        name, '--', 'python3', '-c', code], check=True, stdout=subprocess.DEVNULL)
    roots = [int((lab / 'runs/gvisor' / name / 'launcher-pid.txt').read_text()) for name in names]
    time.sleep(max(0, start-time.time()))
    before = host_cpu(roots)
    time.sleep(max(0, start+duration+.3-time.time()))
    after = host_cpu(roots)
    row = {'trial': trial, 'duration_seconds': duration, 'host_nice': nice, 'workers': workers, 'sandboxes': names, 'usage': []}
    row['host_cpu_seconds'] = [end['seconds']-begin['seconds'] for begin,end in zip(before,after)]
    row['observed_host_nice_values'] = [item['nice_values'] for item in after]
    for name in names:
        logs = lab / 'runs/gvisor' / name
        deadline = time.monotonic() + 35
        while not (logs / 'exit-code.txt').exists():
            if time.monotonic() > deadline:
                raise TimeoutError(name + ': ' + (logs / 'launcher.out').read_text()[-1000:])
            time.sleep(.2)
        lines = (logs / 'guest.out').read_text().splitlines()
        assert not (logs / 'cpu-controller-failed.txt').exists(), 'CPU controller failed: ' + name
        measured = [json.loads(line.removeprefix('CPU_RESULT ')) for line in lines if line.startswith('CPU_RESULT ')]
        assert (logs / 'exit-code.txt').read_text().strip() == '0' and len(measured) == 1, lines[-10:]
        row['usage'].append(measured[0])
    row['work_ratio'] = row['usage'][0]['batches'] / row['usage'][1]['batches'] if row['usage'][1]['batches'] else None
    row['host_cpu_ratio'] = row['host_cpu_seconds'][0] / row['host_cpu_seconds'][1] if row['host_cpu_seconds'][1] else None
    results.append(row)
    print(json.dumps(row), flush=True)
(lab / ('runs/gvisor/cpu-idle-results.json' if '--idle' in sys.argv else ('runs/gvisor/cpu-broker-long-results.json' if duration > 8 else 'runs/gvisor/cpu-broker-results.json') if broker else 'runs/gvisor/cpu-sharing-results.json')).write_text(json.dumps(results, indent=2) + '\n')

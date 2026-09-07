#!/usr/bin/env python3
"""Disposable Slurm acceptance: two MPS guests, owner exit, failure and default GPU."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import gvisor_mps as mps

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--gpu', required=True, type=int)
parser.add_argument('--prefix', default='mps-accept-' + str(time.time_ns()))
args = parser.parse_args()
lab = Path(__file__).resolve().parent.parent
local = Path((lab / 'runs/local-path.txt').read_text().strip())
staged = lab / 'tools/gpu/probes/gpu-mps-client-probe.py'
shutil.copyfile(lab / 'scripts/gpu-mps-client-probe.py', staged)
staged.chmod(0o644)
processes, outputs = [], []
evidence = {'gpu': mps.gvisor_gpu.device_identity(args.gpu), 'guests': {}}


def launch(suffix, chunks, command):
    name = args.prefix + '-' + suffix
    logs = lab / 'runs/gvisor' / name
    logs.mkdir(parents=True)
    output = (logs / 'acceptance-launcher.out').open('wb')
    outputs.append(output)
    options = ['--experimental-gpu-sm-chunks', str(chunks),
               '--experimental-gpu-client-memory-mib', '1024'] if chunks else []
    cmd = [sys.executable, str(lab / 'scripts/run-gvisor.py'), '--gpu', str(args.gpu),
           '--no-runtime-debug', '--network-policy', 'offline', *options, name, '--', *command]
    child = subprocess.Popen(cmd, cwd=lab, stdout=output, stderr=subprocess.STDOUT)
    processes.append(child)
    evidence['guests'][suffix] = {'name': name, 'command': cmd}
    return child, logs


def wait_for(test, child, seconds=30):
    deadline = time.monotonic() + seconds
    while not test():
        if child.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError('guest failed or timed out; inspect acceptance-launcher.out')
        time.sleep(.1)


def successful(logs):
    path = logs / 'guest.out'
    return path.exists() and '"passed": true' in path.read_text()


def record(suffix, logs):
    evidence['guests'][suffix]['output'] = (logs / 'guest.out').read_text()
    evidence['guests'][suffix]['released'] = (logs / 'mps-released.txt').exists()


try:
    a, a_logs = launch('a', 1, ['runuser', '-l', 'ga', '-c',
        'python3 /opt/engine-gpu/probes/gpu-mps-client-probe.py --expect-sms 4 --memory-limit-mib 1024 --hold-seconds 8 --inspect'])
    wait_for(lambda: successful(a_logs), a)
    b, b_logs = launch('b', 2, ['runuser', '-l', 'ga', '-c',
        'python3 /opt/engine-gpu/probes/gpu-mps-client-probe.py --expect-sms 8 --memory-limit-mib 1024 --hold-seconds 15 --inspect'])
    wait_for(lambda: successful(b_logs), b)
    a_meta = json.loads((a_logs / 'mps.json').read_text())
    b_meta = json.loads((b_logs / 'mps.json').read_text())
    assert a_meta['server'] == b_meta['server'] and a_meta['partition'] != b_meta['partition']
    root = local / 'gvisor' / f'mps-{args.gpu}'
    evidence['concurrent_partitions'] = mps.control(root, evidence['gpu']['uuid'], 'lspart')
    assert evidence['concurrent_partitions'].count('Yes') == 2
    assert a.wait(timeout=20) == 0 and b.poll() is None
    state = json.loads((root / 'service.json').read_text())
    assert state['server'] == b_meta['server'] and mps.alive(state['server'])
    assert a_logs.name not in state['leases'] and b_logs.name in state['leases']
    evidence['first_owner_exit_preserves_peer'] = True
    assert b.wait(timeout=25) == 0
    assert not (root / 'service.json').exists()
    for suffix, logs in [('a', a_logs), ('b', b_logs)]:
        record(suffix, logs)
    fault, logs = launch('failure', 1, ['/bin/sleep', '300'])
    wait_for(lambda: (logs / 'launch.json').exists(), fault)
    metadata = json.loads((logs / 'mps.json').read_text())
    assert mps.alive(metadata['server'])
    start = time.monotonic()
    os.kill(metadata['server']['pid'], signal.SIGTERM)
    assert fault.wait(timeout=20) != 0
    assert (logs / 'mps-failed.txt').exists() and (logs / 'mps-released.txt').exists()
    assert not (root / 'service.json').exists()
    evidence['server_failure_terminated_guest_seconds'] = time.monotonic() - start
    default, logs = launch('default', None, ['/bin/bash', '-c',
        'test -z "${CUDA_MPS_SM_PARTITION:-}" && '
        'python3 /opt/engine-gpu/probes/gpu-device-probe.py && '
        'runuser -u ga -- env -u LD_LIBRARY_PATH -u LD_PRELOAD python3 /opt/engine-gpu/probes/gpu-driver-probe.py'])
    assert default.wait(timeout=30) == 0
    record('default', logs)
    assert not (root / 'service.json').exists()
    evidence['passed'] = True
    destination = lab / 'runs' / (args.prefix + '.json')
    destination.write_text(json.dumps(evidence, indent=2) + '\n')
    print(destination)
finally:
    for child in processes:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=20)
    for output in outputs:
        output.close()

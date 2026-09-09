#!/usr/bin/env python3
"""Acquire an owned CPU worker and test remote public API semantics end to end."""
import json
import argparse
from pathlib import Path
import subprocess
import time

from sandweave import Sandbox, Slurm

started = time.monotonic()
parser = argparse.ArgumentParser()
parser.add_argument('--job')
parser.add_argument('--gpu', default='auto')
args = parser.parse_args()
root = Path('runs/sdk-acceptance/targets')
root.mkdir(parents=True, exist_ok=True)
allocation = Slurm.connect(args.job, cpus=4) if args.job else Slurm.acquire(
    gpu=True if args.gpu == 'auto' else args.gpu, cpus=4, memory='32GiB', partition='preempt', qos='preempt_qos',
    account='swelleck', walltime='30m', queue_timeout=300)
with allocation:
    report = {'job_id': allocation.job_id, 'queue_seconds': allocation.queue_seconds}
    (root / 'allocation.json').write_text(json.dumps(report, indent=2))
    print('Allocated CPU worker', allocation.job_id, flush=True)
    with Sandbox(target=allocation) as env:
        assert env.run("python -c 'print(42)'").stdout == '42\n'
        assert env.run('test ! -e /dev/kvm').returncode == 0
        env.files.write_text('/workspace/remote', 'remote bytes €')
        baseline = env.snapshot(state='filesystem')
        with Sandbox.connect(env.id, target=allocation) as borrowed:
            assert borrowed.files.read_text('/workspace/remote') == 'remote bytes €'
        env.pause(); env.resume()
        report['sandbox_id'] = env.id
        report['worker'] = env._connection.call('ping')['hostname']
    assert allocation._job()['JobState'] == 'RUNNING', 'sandbox cleanup cancelled its placement'
    with Sandbox(cache=baseline, target=allocation) as restored:
        assert restored.files.read_text('/workspace/remote') == 'remote bytes €'
    report['seconds'] = time.monotonic()-started
    (root / 'result.json').write_text(json.dumps(report, indent=2))
print('Remote placement acceptance passed', flush=True)

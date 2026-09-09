#!/usr/bin/env python3
"""Qualify CLI allocation ownership and actual cross-worker cache/pool placement."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from sandweave import Sandbox, Pool, Slurm


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = {'complete': False, 'borrowed_job': args.job}
    allocation = None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def cli(*args):
        result = subprocess.run([sys.executable, '-m', 'sandweave.cli', *args], capture_output=True, text=True, timeout=180)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None
    try:
        allocation = cli('slurm', 'acquire', '--gpu', 'auto', '--cpus', '4', '--memory', '16GiB',
                         '--partition', 'preempt', '--qos', 'preempt_qos', '--account', 'swelleck',
                         '--walltime', '15m', '--queue-timeout', '120')
        report['owned_job'] = allocation['job_id']
        args.output.write_text(json.dumps(report, indent=2))
        targets = [Slurm.connect(args.job, cpus=12),
                   Slurm.connect(allocation['job_id'], **allocation['config'])]
        with Sandbox(target=targets[0]) as env:
            env.files.write_text('/workspace/value', 'cross-worker €')
            baseline = env.snapshot(state='filesystem')
            assert baseline.verify()['status'] == 'passed'
            workspace = env.status()['workspace']
        with Sandbox(cache=baseline.id, target=targets[1]) as clone:
            assert clone.files.read_text('/workspace/value') == 'cross-worker €'
            assert clone.status()['workspace'] != workspace
            report['import'] = {'source_workspace': workspace, 'destination_workspace': clone.status()['workspace']}
        with Pool(cache=baseline.id, targets=targets, size=2, warm=2) as pool:
            def evaluate(env, value):
                assert env.files.read_text('/workspace/value') == 'cross-worker €'
                env.files.write_text('/workspace/value', str(value))
                return env._connection.call('ping')['hostname'], env.status()['workspace'], value
            results = list(pool.map(evaluate, range(6)))
            assert [row[2] for row in results] == list(range(6))
            assert len({row[1] for row in results}) == 2
            report['pool'] = results
        report['complete'] = True
        connection = targets[0].connection()
        try:
            connection.call('_shutdown_if_idle')
        finally:
            connection.close()
    finally:
        if allocation:
            cli('slurm', 'close', allocation['job_id'])
            report['owned_job_cancelled'] = True
        assert Slurm.connect(args.job)._job()['JobState'] == 'RUNNING'
        report['borrowed_job_preserved'] = True
        args.output.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()

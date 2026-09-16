#!/usr/bin/env python3
"""Exercise original Harbor tasks through the public Benchmark pull interface."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import shlex
import tarfile
import tempfile
import time

from sandweave import Benchmark


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', help='dataset name, task path, or JSON dataset configuration')
    parser.add_argument('--task', action='append', help='Harbor task filter; repeat to select several')
    parser.add_argument('--oracle', action='store_true', help='run each original solution/solve.sh')
    parser.add_argument('--target')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expect-reward', type=float)
    parser.add_argument('--reward-name', default='reward')
    parser.add_argument('--guest-tools-sha256', help='verify the guest helper selected for a service task')
    args = parser.parse_args()
    source = json.loads(args.source) if args.source.startswith('{') else args.source
    if args.task:
        if isinstance(source, dict):
            source = {**source, 'task_names': args.task}
        else:
            name, _, ref = source.partition('@')
            source = {'name': name, 'task_names': args.task}
            if ref:
                source['ref' if '/' in name else 'version'] = ref
    bench = Benchmark('harbor', source=source, target=args.target, capacity=1)
    report = {'source': source, 'oracle': args.oracle, 'tasks': []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        while True:
            started = time.monotonic()
            try:
                task = next(bench)
            except StopIteration:
                break
            row = {'task': task.id, 'startup_seconds': time.monotonic() - started}
            report['tasks'].append(row)
            try:
                env = task.env
                row['description'] = task.instruction
                row['environment'] = env.run('uname -a; cat /etc/os-release; pwd').stdout
                if args.guest_tools_sha256:
                    row['guest_tools_sha256'] = hashlib.sha256(
                        env.files.read_bytes('/.sandweave-runtime/tools/guest-tools')).hexdigest()
                    assert row['guest_tools_sha256'] == args.guest_tools_sha256
                if args.oracle:
                    solution = Path(task.spec.metadata['path']) / 'solution'
                    with tempfile.TemporaryDirectory(prefix='harbor-oracle-') as temp:
                        archive = Path(temp) / 'solution.tar'
                        with tarfile.open(archive, 'w') as tar:
                            tar.add(solution, arcname='.')
                        env.files.upload(archive, '/tmp/sandweave-oracle.tar')
                    env.run('mkdir -p /solution && tar -xf /tmp/sandweave-oracle.tar '
                            '-C /solution && rm /tmp/sandweave-oracle.tar', user='root', check=True)
                    result = env.run('bash ' + shlex.quote('/solution/solve.sh'), timeout=1800)
                    row['solution'] = {'returncode': result.returncode,
                                       'stdout': result.stdout, 'stderr': result.stderr}
                    if result.returncode:
                        raise RuntimeError('Original solution failed: ' + result.stderr)
                row['evaluation'] = asdict(task.evaluate())
                if args.expect_reward is not None:
                    assert row['evaluation']['rewards'][args.reward_name] == args.expect_reward, row['evaluation']
                print(json.dumps(row), flush=True)
            finally:
                task.close()
                row['total_seconds'] = time.monotonic() - started
                args.output.write_text(json.dumps(report, indent=2) + '\n')
    finally:
        bench.close()


if __name__ == '__main__':
    main()

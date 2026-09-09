#!/usr/bin/env python3
"""Run from outside the checkout with the installed wheel and example provider."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import statistics
import time

import sandweave
from sandweave import Sandbox, Pool, Slurm
from sandweave.sandbox.workspace import engine_sources, home


def distribution(values):
    values = sorted(values)
    return {'n': len(values), 'p50_ms': statistics.median(values)*1000,
            'p95_ms': values[int(.95*(len(values)-1))]*1000,
            'p99_ms': values[int(.99*(len(values)-1))]*1000, 'max_ms': values[-1]*1000}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert 'site-packages' in sandweave.__file__, sandweave.__file__
    assert engine_sources().name == '_engine', engine_sources()
    target = Slurm.connect(args.job, cpus=12)
    report = {'package': sandweave.__file__, 'engine': str(engine_sources()), 'complete': False,
              'scope': 'installed wheel; remote worker in an existing allocation; no queue time hidden in timings'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        cold, commands, direct, cached, checkout, first = [], [], [], [], [], []
        for i in range(20):
            started = time.perf_counter()
            with Sandbox(target=target) as env:
                # Includes construction, readiness and the first real command.
                assert env.run("python -c 'print(2+2)'").stdout == '4\n'
                cold.append(time.perf_counter()-started)
                if i == 0:
                    report['first_worker_and_guest_ms'] = cold[-1]*1000
                    baseline = env.snapshot(state='filesystem')
        with Sandbox(target=target) as env:
            for i in range(100):
                started = time.perf_counter(); assert env.run('true').returncode == 0
                commands.append(time.perf_counter()-started)
                started = time.perf_counter(); assert env.run(argv=['true']).returncode == 0
                direct.append(time.perf_counter()-started)
        for _ in range(20):
            started = time.perf_counter()
            with Sandbox(cache=baseline.id, target=target) as env:
                assert env.run('true').returncode == 0
                cached.append(time.perf_counter()-started)
        with Pool(cache=baseline.id, target=target, size=3, warm=2) as pool:
            for _ in range(30):
                started = time.perf_counter()
                with pool.acquire() as env:
                    checkout.append(time.perf_counter()-started)
                    assert env.run('true').returncode == 0
                    first.append(time.perf_counter()-started)
                # Ready checkout and refill are measured separately, not hidden
                # in reported call timing or represented as unlimited throughput.
                refill_started = time.perf_counter()
                with pool.condition:
                    while len(pool.idle) < 2:
                        pool.condition.wait(.05)
                report.setdefault('refill_seconds', []).append(time.perf_counter()-refill_started)
        report['latency'] = {name: distribution(values) for name, values in
            (('cold_after_worker_first_command', cold[1:]), ('guest_shell_true', commands),
             ('direct_true', direct), ('filesystem_restore_first_command', cached),
             ('ready_pool_checkout', checkout), ('ready_pool_checkout_first_command', first),
             ('pool_refill_wait', report.pop('refill_seconds')))}

        with Sandbox(target=target) as env:
            process = env.exec(argv=['python', '-u', '-c',
                'import os; print(os.isatty(0)); print("reply:"+input())'], pty=True)
            assert process.stdout.readline().strip() == 'True'
            process.resize(30, 100)
            process.stdin.write('installed terminal\n')
            assert process.stdout.readline().strip() == 'installed terminal'
            assert process.stdout.readline().strip() == 'reply:installed terminal'
            assert process.wait(timeout=5) == 0
            async def files():
                async with await env.files.open.aio('/workspace/async', 'w') as stream:
                    await stream.write.aio('installed €\n')
                async with await env.files.open.aio('/workspace/async') as stream:
                    assert [line async for line in stream] == ['installed €\n']
            asyncio.run(files())
            report['terminal_and_async_files'] = 'passed'

        recipe = {'extends': 'coding', 'capabilities': {'robotics': {'provider': 'pointmass', 'dt': .1}}}
        with Sandbox(template=recipe, target=target) as env:
            assert env.robotics.reset()['position'] == 0
            state = env.robotics.step(2)
            assert abs(state['position']-.02) < 1e-9 and state['steps'] == 1
            saved = env.snapshot(state='filesystem')
            env.robotics.step(2)
            with Sandbox(cache=saved.id, target=target) as clone:
                state = clone.capability('robotics').step(0)
                assert abs(state['position']-.04) < 1e-9 and state['steps'] == 2
                clone.pause(); clone.resume()
                assert clone.robotics.step(0)['steps'] == 3
            report['extension'] = env.capabilities['robotics']

        # Connect using SSH metadata instead of the Slurm placement adapter.
        metadata = home() / 'allocations' / ('job-' + args.job) / 'worker.json'
        info = json.loads(metadata.read_text())
        ssh = {'host': info['hostname'], 'metadata': str(metadata)}
        with Sandbox(target=ssh) as env:
            assert env.run('echo ssh').stdout == 'ssh\n'
            report['ssh'] = {'passed': True, 'hostname': info['hostname'], 'provisioning': 'existing owned Slurm worker'}
        # A heterogeneous pool can target the same allocated node through two
        # placements; an independent worker is also required for cross-workspace
        # imports, exercised by the existing target acceptance script.
        with Pool(cache=baseline.id, targets=[target, ssh], size=2, warm=2) as pool:
            assert list(pool.map(lambda env, i: env.run('echo '+str(i)).stdout.strip(), range(4))) == ['0','1','2','3']
        report['installed_sources'] = {
            str(path.relative_to(Path(sandweave.__file__).parent)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(Path(sandweave.__file__).parent.rglob('*'))
            if path.is_file() and path.suffix in ('.py', '.toml', '.sh', '.c')}
        report['complete'] = True
    finally:
        args.output.write_text(json.dumps(report, indent=2))
        connection = target.connection()
        try:
            connection.call('_shutdown_if_idle')
        finally:
            connection.close()
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()

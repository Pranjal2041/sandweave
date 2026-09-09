#!/usr/bin/env python3
"""Exercise the README's 32-lease, eight-warm sandbox pool with real guests."""
import argparse
import json
from pathlib import Path
import threading
import time

from sandweave import Pool, Slurm


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    target = Slurm.connect(args.job, cpus=12)
    report = {'size': 32, 'warm': 8, 'tasks': 64, 'complete': False}
    lock, identities = threading.Lock(), set()
    first_wave = threading.Barrier(32)
    active = peak = 0
    def evaluate(env, index):
        nonlocal active, peak
        with lock:
            assert env.id not in identities
            identities.add(env.id)
            active += 1; peak = max(active, peak)
        try:
            if index < 32:
                first_wave.wait(timeout=90)
            assert env.run('test ! -e /workspace/dirty && test ! -e /dev/kvm && test ! -e /dev/nvidia0').returncode == 0
            env.files.write_text('/workspace/dirty', str(index))
            result = env.run("python -c 'print("+str(index)+"*2)'", timeout=20)
            time.sleep(.2)
            assert env.files.read_text('/workspace/dirty') == str(index)
            return int(result.stdout)
        finally:
            with lock:
                active -= 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        with Pool(template='coding', target=target, size=32, warm=8) as pool:
            report['initial_ready_seconds'] = time.monotonic()-started
            assert len(pool.idle) == 8
            began = time.monotonic()
            results = list(pool.map(evaluate, range(64)))
            report['map_seconds'] = time.monotonic()-began
            assert results == [i*2 for i in range(64)]
            assert peak == 32
            report.update(unique_guests=len(identities), peak_callbacks=peak, results=results, complete=True)
    finally:
        report['total_seconds'] = time.monotonic()-started
        args.output.write_text(json.dumps(report, indent=2))
        connection = target.connection()
        try:
            connection.call('_shutdown_if_idle')
        finally:
            connection.close()
    print(json.dumps({k: v for k, v in report.items() if k != 'results'}, indent=2), flush=True)


if __name__ == '__main__':
    main()

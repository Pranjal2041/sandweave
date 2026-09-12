#!/usr/bin/env python3
"""Compare host subprocess creation with the worker's isolated launcher."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sandweave.sandbox.launcher import install


def measure(count, concurrency, active):
    children = []
    def command(_):
        started = time.perf_counter()
        result = subprocess.run(['/bin/echo', 'worker launch'], capture_output=True, check=True)
        assert result.stdout == b'worker launch\n'
        return time.perf_counter() - started
    try:
        for _ in range(active):
            children.append(subprocess.Popen(['/bin/sleep', '120'], stdin=subprocess.DEVNULL,
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        serial = [command(i) for i in range(100)]
        with ThreadPoolExecutor(concurrency) as executor:
            started = time.perf_counter()
            times = sorted(executor.map(command, range(count)))
            elapsed = time.perf_counter() - started
        return {'serial_median_seconds': statistics.median(serial), 'concurrent_seconds': elapsed,
                'requests_per_second': count / elapsed, 'concurrent_p50_seconds': statistics.median(times),
                'concurrent_p95_seconds': times[int(.95 * (len(times) - 1))]}
    finally:
        for child in children:
            child.terminate()
        for child in children:
            child.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--count', type=int, default=256)
    parser.add_argument('--concurrency', type=int, default=32)
    parser.add_argument('--active', type=int, default=128)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.count < 1 or args.concurrency < 1 or args.active < 0:
        parser.error('count/concurrency must be positive and active must be nonnegative')
    results = {'count': args.count, 'concurrency': args.concurrency, 'active': args.active}
    results['native'] = measure(args.count, args.concurrency, args.active)
    install()
    results['isolated'] = measure(args.count, args.concurrency, args.active)
    text = json.dumps(results, indent=2) + '\n'
    if args.output:
        args.output.write_text(text)
    print(text, end='')


if __name__ == '__main__':
    main()

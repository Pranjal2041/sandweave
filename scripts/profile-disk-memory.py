#!/usr/bin/env python3
"""Run a native mmap experiment inside a private, memory-limited Slurm step.

This measures Linux paging, not an integrated Sandweave guest-memory backend.
Each case creates a fully written private file and validates a cold page cache.
The inside mode refuses to run without the requested hard memory limit and
disabled swap. It does not modify cgroups or drop other processes' caches.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time


def read(path):
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return None


def memory_group(limit):
    membership = Path('/proc/self/cgroup').read_text().strip()
    if not membership.startswith('0::/'):
        raise RuntimeError('this profiling runner requires cgroup v2')
    current = Path('/sys/fs/cgroup') / membership.split('::', 1)[1].lstrip('/')
    groups = [p for p in [current, *current.parents] if str(p).startswith('/sys/fs/cgroup/')]
    for group in groups:
        if read(group / 'memory.max') == str(limit):
            if not any(read(p / 'memory.swap.max') == '0' for p in groups):
                raise RuntimeError('swap must be disabled for this experiment')
            return group, groups
    raise RuntimeError(f'expected a private cgroup with memory.max={limit}')


def inside(args):
    directory = Path(args.inside)
    limit = args.limit_mib * 1024**2
    group, ancestors = memory_group(limit)
    metadata = {
        'hostname': os.uname().nodename, 'kernel': os.uname().release,
        'affinity': sorted(os.sched_getaffinity(0)), 'uid': os.getuid(),
        'memory_group': str(group), 'limit_bytes': limit,
        'ancestors': {str(p): {n: read(p / n) for n in
                              ('memory.max', 'memory.high', 'memory.swap.max')}
                      for p in ancestors},
        'size_mib': args.size_mib, 'hot_mib': args.hot_mib,
        'operations': args.operations, 'repeats': args.repeats, 'anonymous': args.anonymous,
        'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    (directory / 'metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
    stop = threading.Event()
    def sample():
        with (directory / 'memory.jsonl').open('w') as output:
            while not stop.is_set():
                row = {'monotonic': time.monotonic()}
                for field in ('memory.current', 'memory.peak', 'memory.swap.current',
                              'memory.events', 'memory.stat', 'memory.pressure', 'io.stat'):
                    row[field] = read(group / field)
                output.write(json.dumps(row) + '\n')
                output.flush()
                stop.wait(.1)
    sampler = threading.Thread(target=sample)
    sampler.start()
    command = [str(directory.parent / 'probe'), str(directory / 'backing.bin'),
               str(args.size_mib), str(args.hot_mib), str(args.operations), str(args.repeats)]
    if args.anonymous:
        command.append('--anonymous')
    try:
        with (directory / 'stderr.log').open('w') as errors, (directory / 'results.jsonl').open('w') as output:
            child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, text=True)
            for line in child.stdout:
                output.write(line); output.flush()
                print(line, end='', flush=True)
            code = child.wait()
        if code:
            raise RuntimeError(f'probe exited {code}; see {directory / "stderr.log"}')
    finally:
        stop.set(); sampler.join()
    events = dict(line.split() for line in (group / 'memory.events').read_text().splitlines())
    if int(events['oom']) or int(events['oom_kill']):
        raise RuntimeError('experiment triggered OOM; results must not be accepted')
    (directory / 'complete.json').write_text(json.dumps({'events': events,
        'peak_bytes': read(group / 'memory.peak')}, indent=2) + '\n')
    # Only the file created by this case is removed. Keep all small evidence.
    if not args.anonymous:
        (directory / 'backing.bin').unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', help='existing Slurm allocation on the selected scratch host')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--scratch', type=Path)
    parser.add_argument('--inside')
    parser.add_argument('--anonymous', action='store_true')
    parser.add_argument('--limit-mib', type=int)
    parser.add_argument('--size-mib', type=int, default=20480)
    parser.add_argument('--hot-mib', type=int, default=2048)
    parser.add_argument('--operations', type=int, default=200000)
    parser.add_argument('--repeats', type=int, default=3)
    args = parser.parse_args()
    if args.inside:
        inside(args); return
    if not args.job or not args.output or not args.scratch:
        parser.error('--job, --output and --scratch are required')
    if not args.scratch.is_absolute() or not args.output.is_absolute():
        parser.error('scratch and output must be absolute paths')
    if not 0 < args.hot_mib < args.size_mib or args.operations < 100 or args.repeats < 1:
        parser.error('invalid workload sizes')
    args.output.mkdir(parents=True, exist_ok=False)
    args.scratch.mkdir(parents=True, exist_ok=False)
    script = Path(__file__).resolve()
    source = script.with_name('disk-memory-probe.c')
    shutil.copy2(script, args.scratch / script.name)
    shutil.copy2(source, args.scratch / source.name)
    build = ['gcc', '-O3', '-march=native', '-Wall', '-Wextra', '-Werror',
             str(source), '-o', str(args.scratch / 'probe')]
    subprocess.run(build, check=True)
    manifest = {'build': build, 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                'runner_sha256': hashlib.sha256(script.read_bytes()).hexdigest(),
                'job': args.job, 'scratch': str(args.scratch),
                'storage': subprocess.check_output(['findmnt', '-T', str(args.scratch), '-J'], text=True)}
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    # Same 20 GiB workload; change only the total cgroup memory allowance.
    cases = [('ram', args.size_mib + 4096), ('resident', args.size_mib + 4096),
             ('overflow', args.size_mib // 5)]
    for label, limit in cases:
        directory = args.scratch / label
        directory.mkdir()
        command = ['srun', '--jobid=' + args.job, '--overlap', '--exact', '--nodes=1',
                   '--ntasks=1', '--cpus-per-task=2', f'--mem={limit}M', '--gres=none',
                   '--time=00:30:00', '/usr/bin/python3', '-u', str(args.scratch / script.name),
                   '--inside', str(directory), '--limit-mib', str(limit),
                   '--size-mib', str(args.size_mib), '--hot-mib', str(args.hot_mib),
                   '--operations', str(args.operations), '--repeats', str(args.repeats)]
        if label == 'ram':
            command.append('--anonymous')
        print(json.dumps({'case': label, 'command': command}), flush=True)
        try:
            subprocess.run(command, check=True, timeout=1900)
        finally:
            destination = args.output / label
            destination.mkdir()
            for name in ('metadata.json', 'memory.jsonl', 'results.jsonl', 'stderr.log', 'complete.json'):
                if (directory / name).exists():
                    shutil.copy2(directory / name, destination / name)


if __name__ == '__main__':
    main()

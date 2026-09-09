"""Worker memory reservation; CPU sharing remains the runtime broker's job."""
import os
from pathlib import Path

from .errors import ResourceUnavailable
from .resources import memory_bytes


def budget():
    if os.environ.get('SANDWEAVE_MEMORY_BUDGET'):
        return memory_bytes(os.environ['SANDWEAVE_MEMORY_BUDGET'])
    if os.environ.get('SLURM_MEM_PER_NODE'):
        return int(os.environ['SLURM_MEM_PER_NODE']) * 1024**2
    if os.environ.get('SLURM_MEM_PER_CPU'):
        return int(os.environ['SLURM_MEM_PER_CPU']) * len(os.sched_getaffinity(0)) * 1024**2
    information = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    return int(information['MemAvailable'].split()[0]) * 1024


def reservation(spec):
    return sum(memory_bytes(value) for value in spec['resources']['memory'].values())


def admit(worker, spec):
    """Caller holds the worker guard until the new record has been published."""
    reserved = 0
    for path in worker.records.glob('*.bin'):
        record = worker.read(path.stem)
        if record['state'] in ('terminated', 'stopped'):
            continue
        if record['state'] not in ('creating', 'preparing'):
            if worker.runtime.status(record['id'])['status'] not in ('starting', 'running', 'paused'):
                continue
        if spec.get('name') and record.get('name') == spec['name']:
            raise FileExistsError('a live sandbox already has this name: ' + spec['name'])
        reserved += reservation(record['spec'])
    requested = reservation(spec)
    if requested + reserved > worker.memory_budget:
        raise ResourceUnavailable(f'worker memory budget exhausted: requested {requested}, reserved {reserved}, '
                                  f'budget {worker.memory_budget} bytes; guest and runtime budgets are both counted')
    return {'memory_reserved': requested, 'worker_memory_budget': worker.memory_budget,
            'cpu_policy': 'shared eligible CPU pool; advertised vCPUs do not reserve physical cores'}

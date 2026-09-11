"""Worker memory reservation; CPU sharing remains the runtime broker's job."""
import os
from pathlib import Path

from .errors import ResourceUnavailable
from .resources import memory_bytes


def budget():
    if os.environ.get('SANDWEAVE_MEMORY_BUDGET'):
        requested = memory_bytes(os.environ['SANDWEAVE_MEMORY_BUDGET'])
    elif os.environ.get('SLURM_MEM_PER_NODE'):
        requested = int(os.environ['SLURM_MEM_PER_NODE']) * 1024**2
    elif os.environ.get('SLURM_MEM_PER_CPU'):
        requested = int(os.environ['SLURM_MEM_PER_CPU']) * len(os.sched_getaffinity(0)) * 1024**2
    else:
        information = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        requested = int(information['MemAvailable'].split()[0]) * 1024
    limit = cgroup_limit()
    return min(requested, limit) if limit is not None else requested


def cgroup_limit(proc=Path('/proc')):
    """Read visible v1/v2 hard limits, including narrower ancestor limits.

    A scheduler's environment can describe an entire job while this process
    belongs to a smaller step. An explicit budget cannot expand that step.
    """
    import re
    def unescape(value):
        return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)
    try:
        memberships = [line.split(':', 2) for line in (proc / 'self/cgroup').read_text().splitlines()]
        mounts = (proc / 'self/mountinfo').read_text().splitlines()
    except OSError:
        return None
    limits = []
    for line in mounts:
        fields, separator, filesystem = line.partition(' - ')
        if not separator:
            continue
        fields, filesystem = fields.split(), filesystem.split()
        if len(fields) < 5 or len(filesystem) < 3:
            continue
        version = filesystem[0]
        if version != 'cgroup2' and not (version == 'cgroup' and 'memory' in filesystem[2].split(',')):
            continue
        root, mount = Path(unescape(fields[3])), Path(unescape(fields[4]))
        for membership in memberships:
            if len(membership) != 3:
                continue
            _, controllers, member = membership
            if not (controllers == '' if version == 'cgroup2' else 'memory' in controllers.split(',')):
                continue
            member = Path(member)
            try:
                relative = member.relative_to(root)
            except ValueError:
                # A cgroup namespace may make membership relative to its own
                # root while the visible mount names the host subtree.
                relative = member.relative_to('/')
            if '..' in relative.parts:
                continue
            directory = mount / relative
            while directory.is_relative_to(mount):
                filename = 'memory.max' if version == 'cgroup2' else 'memory.limit_in_bytes'
                try:
                    value = (directory / filename).read_text().strip()
                    if value != 'max' and 0 <= int(value) < 2**60:
                        limits.append(int(value))
                    if version == 'cgroup':
                        stats = dict(row.split() for row in (directory / 'memory.stat').read_text().splitlines())
                        inherited = int(stats.get('hierarchical_memory_limit', 2**63))
                        if 0 <= inherited < 2**60:
                            limits.append(inherited)
                        if 'hierarchical_memory_limit' in stats:
                            # v1 can disable hierarchical accounting. The
                            # kernel's effective value handles that case.
                            break
                except (OSError, ValueError):
                    pass
                if directory == mount:
                    break
                directory = directory.parent
    return min(limits) if limits else None


def reservation(spec):
    memory = spec['resources']['memory']
    if memory.get('disk') is not None:
        return sum(((memory_bytes(memory[key]) + 1024**2 - 1) // 1024**2) * 1024**2
                   for key in ('guest', 'runtime'))
    return memory_bytes(memory['guest']) + memory_bytes(memory['runtime'])


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

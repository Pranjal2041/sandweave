"""Private disk backing and kernel-enforced RAM limits for gVisor launches.

The supervisor stays outside the sandbox's memory cgroup. It never moves other
processes, enables host swap, or changes an existing cgroup's limits.
"""
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

import cpu_broker
import snapshot_store


def cgroup():
    member = next((line.split(':', 2)[2] for line in Path('/proc/self/cgroup').read_text().splitlines()
                   if line.startswith('0::')), None)
    if member is None:
        raise RuntimeError('disk memory requires a cgroup-v2 memory controller')
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields, _, filesystem = line.partition(' - ')
        if filesystem.split()[0] != 'cgroup2':
            continue
        fields = fields.split()
        decode = lambda value: re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)
        root, mount = Path(decode(fields[3])), Path(decode(fields[4]))
        path = Path(member)
        relative = path.relative_to(root) if path.is_relative_to(root) else path.relative_to('/')
        if '..' not in relative.parts:
            return mount / relative, mount
    raise RuntimeError('the memory cgroup filesystem is not accessible')


def verify_limit(expected):
    """Require a dedicated finite limit before creating or touching guest pages."""
    path, mount = cgroup()
    while True:
        value = (path / 'memory.max').read_text().strip()
        if value != 'max':
            actual = int(value)
            if actual > expected:
                raise RuntimeError(f'memory controller granted {actual} bytes; requested {expected}')
            if actual < expected:
                raise RuntimeError(f'memory controller provides only {actual} of {expected} requested bytes')
            if (path / 'memory.swap.max').read_text().strip() != '0':
                raise RuntimeError('disk memory requires host swap disabled in its dedicated cgroup')
            return path
        if path == mount:
            raise RuntimeError('the memory controller did not enforce the requested RAM limit')
        path = path.parent


def delegate(limit):
    """Use an already delegated subtree, without escaping an allocation."""
    current, mount = cgroup()
    # A delegated parent may host sibling task cgroups. Never traverse above
    # a root-owned boundary to a less restrictive allocation or user.slice.
    candidates = [current]
    parent = current.parent
    while parent != mount and parent.stat().st_uid == os.getuid():
        candidates.append(parent)
        parent = parent.parent
    for parent in candidates:
        if not os.access(parent, os.W_OK):
            continue
        directory = None
        try:
            # Do not change subtree_control: a populated parent cannot safely
            # enable it, and moving the worker would affect existing workloads.
            if 'memory' not in (parent / 'cgroup.subtree_control').read_text().split():
                continue
            directory = Path(tempfile.mkdtemp(prefix='sandweave-memory-', dir=parent))
            (directory / 'memory.max').write_text(str(limit))
            (directory / 'memory.high').write_text(str(limit))
            (directory / 'memory.swap.max').write_text('0')
            (directory / 'memory.oom.group').write_text('1')
            return directory
        except OSError:
            if directory is not None:
                directory.rmdir()
    return None


def slurm_command(limit, cpus, gpu=False):
    job = os.environ.get('SLURM_JOB_ID')
    if not job or not re.fullmatch(r'[0-9]+', job) or not shutil.which('srun'):
        raise RuntimeError('Disk memory needs a delegated memory cgroup or a Slurm allocation '
                           'with enforced step memory limits. This worker provides neither.')
    node = os.environ.get('SLURMD_NODENAME') or socket.gethostname()
    count = allocation_cpus(job, node)
    mask = hex(sum(1 << cpu for cpu in cpus))
    # A narrow step would let Slurm pick a different subset of the allocation.
    # Share the node's allocated CPUs and bind the task to the worker's mask.
    # Never acquire/cancel the job or expand the worker's effective CPU pool.
    return ['srun', '--jobid=' + job, '--overlap', '--exact', '--nodes=1', '--ntasks=1',
            '--nodelist=' + node, '--cpus-per-task=' + str(count),
            '--mem=' + str(limit // 1024**2) + 'M', '--cpu-bind=mask_cpu:' + mask, '--kill-on-bad-exit=1',
            *([] if gpu else ['--gres=none'])]


def allocation_cpus(job, node):
    description = subprocess.check_output(['scontrol', 'show', 'job', '-d', '-o', job], text=True, timeout=15)
    for names, cpus in re.findall(r'\bNodes=(\S+)\s+CPU_IDs=(\S+)', description):
        nodes = [names] if names == node else subprocess.check_output(
            ['scontrol', 'show', 'hostnames', names], text=True, timeout=15).split()
        if node in nodes:
            return len(cpu_set(cpus))
    raise RuntimeError('could not determine this node\'s allocated CPUs for a memory-limited step')


def cpu_set(value):
    result = set()
    for part in value.split(','):
        bounds = list(map(int, part.split('-')))
        if len(bounds) > 2 or bounds[0] < 0 or bounds[-1] < bounds[0]:
            raise ValueError('invalid CPU pool')
        result.update(range(bounds[0], bounds[-1] + 1))
    return result


def supervise(args, lab, local):
    """Run the real launcher in its cap; clean only our private empty folder."""
    limit = (args.ram_mib + args.runtime_memory_mib) * 1024**2
    base = Path(args.disk_path)
    if not base.is_absolute() or '..' in base.parts or any(c in str(base) for c in (':', ',', '\n', '\r', '\0')):
        raise ValueError('disk-path must be an absolute worker directory without .., colons or commas')
    base.mkdir(parents=True, exist_ok=True)
    base = base.resolve()
    cpus = sorted(cpu_set(args.cpus))
    if not set(cpus) <= os.sched_getaffinity(0):
        raise ValueError('requested CPU pool exceeds this worker allocation')
    group = delegate(limit)
    prefix = [] if group else slurm_command(limit, cpus, args.gpu is not None)
    directory = None
    try:
        directory = Path(tempfile.mkdtemp(prefix='sandweave-memory-', dir=base))
        logs = lab / 'runs/gvisor' / args.name
        logs.mkdir(parents=True, exist_ok=True)
        record = {'directory': str(directory), 'directory_inode': directory.stat().st_ino,
                  'directory_device': directory.stat().st_dev, 'host_limit_bytes': limit,
                  'backend': 'cgroup' if group else 'slurm', 'cgroup': str(group) if group else None}
        snapshot_store.write_json(logs / 'disk-memory.json', record)
    except BaseException:
        if directory:
            directory.rmdir()
        if group:
            group.rmdir()
        raise
    registration = None
    child = None
    try:
        # Keep the shared CPU controller outside any individual memory cap.
        # Its placeholder also keeps it alive during a slow step launch.
        if args.cpu_policy != 'shared':
            registration = cpu_broker.register(local, args.name + '-memory-supervisor', cpus, 100, None)
        command = [sys.executable, str(lab / 'scripts/run-gvisor.py'),
                   '--disk-memory-inner', str(directory), '--cpus', args.cpus, *sys.argv[1:]]
        environment = dict(os.environ)
        if group:
            environment['SANDWEAVE_MEMORY_CGROUP'] = str(group)
        else:
            # srun must pass through the allocation's device/CPU context.
            environment.pop('SANDWEAVE_MEMORY_CGROUP', None)
        child = subprocess.Popen([*prefix, *command], env=environment, start_new_session=True)
        def stop(signum, frame):
            if child.poll() is None:
                child.send_signal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        return child.wait()
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        if registration is not None:
            registration.unlink(missing_ok=True)
        cleanup(logs)
        if group:
            try:
                group.rmdir()
            except OSError:
                # A concurrent stop can finish reaping the owned runtime.
                pass


def enter(args, logs):
    target = os.environ.pop('SANDWEAVE_MEMORY_CGROUP', None)
    if target:
        (Path(target) / 'cgroup.procs').write_text(str(os.getpid()))
    group = verify_limit((args.ram_mib + args.runtime_memory_mib) * 1024**2)
    if target:
        if group != Path(target):
            raise RuntimeError('RAM limit is not on the dedicated sandbox cgroup')
    else:
        step = os.environ.get('SLURM_STEP_ID', '')
        if not step.isdigit() or ('step_' + step) not in group.parts:
            raise RuntimeError('Slurm did not install a dedicated step memory limit')
    if not cpu_set(args.cpus) <= os.sched_getaffinity(0):
        raise RuntimeError('memory-limited launch did not preserve the worker CPU pool')
    path = logs / 'disk-memory.json'
    record = json.loads(path.read_text())
    if Path(record['directory']) != args.disk_memory_inner:
        raise ValueError('disk memory directory does not match its supervisor')
    record.update(cgroup=str(group), verified_at=time.time())
    snapshot_store.write_json(path, record)


def cleanup(logs):
    try:
        record = json.loads((logs / 'disk-memory.json').read_text())
        path = Path(record['directory'])
        status = path.lstat()
    except FileNotFoundError:
        return
    # Never recursively delete a user-provided path or a replaced directory.
    if status.st_ino == record['directory_inode'] and status.st_dev == record['directory_device']:
        try:
            path.rmdir()
        except FileNotFoundError:
            pass

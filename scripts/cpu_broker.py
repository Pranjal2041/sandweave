"""Unprivileged CPU-time sharing for explicitly registered lab process trees.

This is a sampled userspace controller, not a kernel cgroup. It uses host CPU
accounting and cooperative guest pause/resume intervals; quota overshoot is possible
within a sampling/discovery interval. Runtime and transport CPU is accounted,
but only guest execution is throttled; this is not a hard host CPU ceiling. The launcher itself is never stopped.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import socket

TICK = .02
BURST = .10


def process_table(pids=None):
    result = {}
    paths = Path('/proc').glob('[0-9]*/stat') if pids is None else [Path(f'/proc/{p}/stat') for p in pids]
    for path in paths:
        try:
            text = path.read_text()
            fields = text[text.rfind(')') + 2:].split()
            result[int(path.parent.name)] = {
                'state': fields[0], 'parent': int(fields[1]), 'group': int(fields[2]),
                'start': int(fields[19]), 'cpu': sum(map(int, fields[11:15])) / os.sysconf('SC_CLK_TCK')}
        except (OSError, ValueError):
            pass
    return result


def discover_trees(roots):
    """Discover descendants without scanning unrelated processes on the node."""
    found, pending = set(), list(roots)
    while pending:
        pid = pending.pop()
        if pid in found or pid == os.getpid():
            continue
        found.add(pid)
        try:
            for path in Path(f'/proc/{pid}/task').glob('*/children'):
                try:
                    pending.extend(map(int, path.read_text().split()))
                except (OSError, ValueError):
                    pass
        except OSError:
            pass
    return process_table(found)


def allocate_rates(active, capacity):
    """Weighted water filling: redistribute capacity unused by capped jobs."""
    remaining = list(active)
    rates = {}
    while remaining:
        weights = sum(j.config['weight'] for j in remaining)
        capped = [j for j in remaining if j.config.get('quota') is not None
                  and j.config['quota'] < capacity*j.config['weight']/weights]
        if not capped:
            rates.update({j: capacity*j.config['weight']/weights for j in remaining})
            break
        for job in capped:
            rates[job] = job.config['quota']
            capacity -= rates[job]
            remaining.remove(job)
    return rates


def descendants(table, root):
    found = {root}
    while True:
        extra = {pid for pid, info in table.items() if info['parent'] in found} - found
        if not extra:
            return found
        found.update(extra)


def control_pause(path, paused):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(2)
        conn.connect(str(path))
        conn.sendall(json.dumps({'method': 'containerManager.SetCPUPaused', 'arg': paused}).encode())
        data = b''
        while len(data) < 65536:
            part = conn.recv(4096)
            if not part:
                raise ConnectionError('CPU control socket closed')
            data += part
            try:
                result = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not result['success']:
                raise RuntimeError(result['err'])
            return
        raise ValueError('CPU control reply exceeded limit')


def resume_tree(root, control_path):
    try:
        control_pause(control_path, False)
    except (OSError, ConnectionError):
        pass


def terminate_trees(roots):
    """Stop registered runtime trees, including Apptainer's FUSE helpers."""
    members = discover_trees(roots)
    for pid, original in members.items():
        current = process_table([pid]).get(pid)
        if current is not None and current['start'] == original['start']:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


class Job:
    def __init__(self, config):
        self.config = config
        self.credit = 0.
        self.previous = None
        self.total = 0.
        self.active_until = time.monotonic() + .2
        self.paused = False
        self.stops = 0
        self.ready = False
        self.members = {config['root']}

    def update(self, table, now):
        root = self.config['root']
        info = table.get(root)
        if info is None or info['start'] != self.config['start']:
            return False
        members = descendants(table, root) - {os.getpid()}
        self.members = members
        cpu = sum(table[pid]['cpu'] for pid in members if pid in table)
        if not self.ready:
            try:
                self.ready = json.loads(Path(self.config['state_path']).read_text())['status'] == 'running'
            except (OSError, ValueError, KeyError):
                pass
            self.previous = cpu
            if not self.ready:
                return True
        # Sampling a reaping tree is not atomic. Never count a negative delta
        # or count the same recovered sample a second time.
        delta = 0 if self.previous is None else max(0, cpu-self.previous)
        self.previous = max(cpu, self.previous or 0)
        self.total += delta
        self.credit -= delta
        if delta > .001 or any(table[p]['state'] == 'R' for p in members - {root} if p in table):
            self.active_until = now + .10
        return True

    def set_paused(self, paused, table):
        if paused == self.paused:
            return
        path = Path(self.config['control_path'])
        if not path.exists():
            return
        control_pause(path, paused)
        if paused:
            self.stops += 1
        self.paused = paused

    def close(self, table):
        try:
            self.set_paused(False, table)
        except (OSError, ConnectionError, RuntimeError, ValueError, KeyError):
            pass


def run(directory, cpus):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        jobs = {}
        stop = False
        def shutdown(signum, frame):
            nonlocal stop
            stop = True
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        previous = time.monotonic()
        idle_since = previous
        last_report = 0
        last_discovery = 0
        table = {}
        try:
            while not stop:
                now = time.monotonic()
                elapsed = min(now-previous, .2)
                previous = now
                if now-last_discovery > .25:
                    roots = []
                    for path in directory.glob('job-*.json'):
                        try:
                            roots.append(json.loads(path.read_text())['root'])
                        except (OSError, ValueError):
                            pass
                    table = discover_trees(roots)
                    last_discovery = now
                else:
                    hot = set().union(*(j.members for j in jobs.values())) if jobs else set()
                    for pid in hot:
                        table.pop(pid, None)
                    table.update(process_table(hot))
                for path in directory.glob('job-*.json'):
                    if path.name in jobs:
                        continue
                    try:
                        cfg = json.loads(path.read_text())
                    except (OSError, ValueError):
                        # The launcher may remove its registration after glob.
                        continue
                    if cfg['cpus'] != cpus or cfg['weight'] <= 0:
                        raise ValueError('inconsistent broker registration')
                    if table.get(cfg['root'], {}).get('start') == cfg['start']:
                        jobs[path.name] = Job(cfg)
                for key, job in list(jobs.items()):
                    if not job.update(table, now) or not (directory / key).exists():
                        job.close(table)
                        del jobs[key]
                if jobs:
                    idle_since = now
                elif now-idle_since > 30:
                    break
                active = [j for key,j in jobs.items() if j.ready and not (directory / (key + '.suspend')).exists() and (j.paused or j.active_until > now)]
                rates = allocate_rates(active, len(cpus))
                for key, job in list(jobs.items()):
                    try:
                        if job not in active:
                            job.credit = 0
                            job.set_paused(False, table)
                            continue
                        rate = rates[job]
                        job.credit = min(rate*BURST, job.credit + rate*elapsed)
                        job.set_paused(job.credit < 0, table)
                    except (OSError, ConnectionError, RuntimeError, ValueError, KeyError) as error:
                        # A sandbox can exit between accounting and this RPC.
                        # Invalidate only that job, never the rest of the pool.
                        (directory / (key + '.failed')).write_text(str(error) + '\n')
                        (directory / key).unlink(missing_ok=True)
                        job.close(table)
                        del jobs[key]
                if now-last_report > .25:
                    report = {'pid': os.getpid(), 'time': time.time(), 'tick_seconds': TICK,
                              'cpus': cpus, 'jobs': {key: {'cpu_seconds': j.total, 'credit': j.credit,
                                      'paused': j.paused, 'stops': j.stops} for key,j in jobs.items()}}
                    temp = directory / 'status.tmp'
                    temp.write_text(json.dumps(report))
                    temp.replace(directory / 'status.json')
                    last_report = now
                time.sleep(max(0, TICK-(time.monotonic()-now)))
        finally:
            table = discover_trees([j.config['root'] for j in jobs.values()])
            for job in jobs.values():
                job.close(table)


def register(local, name, cpus, weight, quota):
    # A CPU list can exceed the filesystem's 255-byte component limit on
    # large machines. Registrations retain the full allocation for validation.
    pool = hashlib.sha256(','.join(map(str, sorted(cpus))).encode()).hexdigest()[:24]
    directory = local / 'gvisor/cpu-brokers' / ('pool-' + pool)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = os.getpid()
    info = process_table([root])[root]
    path = directory / ('job-' + name + '.json')
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps({'root': root, 'start': info['start'], 'cpus': sorted(cpus),
                               'weight': weight, 'quota': quota, 'state_path': str(local / 'gvisor/state' / f'{name}_sandbox:{name}.state'), 'control_path': str(local / 'gvisor/state' / ('runsc-' + name + '.sock'))}))
    temp.replace(path)
    with (directory / 'broker.log').open('ab') as output:
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--directory', str(directory),
                          '--cpus', ','.join(map(str, sorted(cpus)))], stdin=subprocess.DEVNULL,
                         stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
    # The launcher's watchdog must not start before the broker acknowledges
    # this registration. A cold Python start on network storage can take more
    # than the watchdog's heartbeat interval.
    deadline = time.monotonic() + 60
    while True:
        try:
            report = json.loads((directory / 'status.json').read_text())
            if path.name in report.get('jobs', {}) and time.time() - report['time'] <= 3:
                return path
        except (OSError, ValueError):
            pass
        code = child.poll()
        # Exit zero may mean another broker already owns this CPU pool.
        if code not in (None, 0) or time.monotonic() >= deadline:
            path.unlink(missing_ok=True)
            raise RuntimeError('CPU controller did not acknowledge registration; see ' + str(directory / 'broker.log'))
        time.sleep(.05)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory', type=Path, required=True)
    p.add_argument('--cpus', required=True)
    args = p.parse_args()
    run(args.directory, sorted(map(int, args.cpus.split(','))))

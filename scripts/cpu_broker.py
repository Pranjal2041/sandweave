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
DEMAND_WINDOW = .10
RUNNABLE_WINDOW = .04
ACCOUNTING_BURST = 2 / os.sysconf('SC_CLK_TCK')


def process_table(pids=None):
    result = {}
    paths = Path('/proc').glob('[0-9]*/stat') if pids is None else [Path(f'/proc/{p}/stat') for p in pids]
    for path in paths:
        try:
            text = path.read_text()
            fields = text[text.rfind(')') + 2:].split()
            result[int(path.parent.name)] = {
                'state': fields[0], 'parent': int(fields[1]), 'group': int(fields[2]),
                'start': int(fields[19]), 'threads': int(fields[17]),
                'cpu': sum(map(int, fields[11:15])) / os.sysconf('SC_CLK_TCK')}
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


def runnable_threads(table, members):
    """Find running AND CPU-waiting threads, including sleeping leaders' peers.

    Sample only registered trees. If a process changes while being inspected,
    return unknown rather than treating unreadable work as idle.
    """
    runnable = set()
    for pid in members:
        info = table.get(pid)
        if info is None:
            return None
        if info['threads'] == 1:
            if info['state'] == 'R':
                runnable.add(pid)
            continue
        try:
            paths = list(Path(f'/proc/{pid}/task').glob('*/stat'))
            if len(paths) != info['threads']:
                return None
            for path in paths:
                stat = path.read_text()
                if stat[stat.rfind(')') + 2:].split()[0] == 'R':
                    runnable.add(int(path.parent.name))
        except (OSError, ValueError, IndexError):
            return None
    return runnable


def _fill_rates(active, capacity, caps, rates):
    remaining = list(active)
    while remaining:
        weights = sum(j.config['weight'] for j in remaining)
        capped = [j for j in remaining if caps[j] is not None
                  and caps[j] - rates[j] < capacity*j.config['weight']/weights]
        if not capped:
            for job in remaining:
                rates[job] += capacity*job.config['weight']/weights
            break
        for job in capped:
            capacity -= caps[job] - rates[job]
            rates[job] = caps[job]
            remaining.remove(job)


def allocate_rates(active, capacity):
    """Share by weight, lending unconsumed shares without relaxing quotas."""
    rates = dict.fromkeys(active, 0.)
    quotas = {j: j.config.get('quota') for j in active}
    caps = {j: min(c for c in (j.demand, quotas[j]) if c is not None)
            if j.demand is not None or quotas[j] is not None else None for j in active}
    _fill_rates(active, capacity, caps, rates)
    # Demand is an observation, not a hard ceiling. If everyone appears to
    # need less than capacity, keep the remainder available for new work.
    spare = max(0., capacity - sum(rates.values()))
    if spare:
        _fill_rates(active, spare, quotas, rates)
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
        self.demand = None
        self.demand_elapsed = 0.
        self.demand_cpu = 0.
        self.demand_sample = None
        self.observed_demand = None
        self.runnable_since = {}
        self.previous_threads = set()
        self.pending_threads = 0
        self.rate = 0.

    def update_demand(self, table, delta, now):
        if self.demand_sample is None:
            self.demand_sample = now
            return
        elapsed = now - self.demand_sample
        self.demand_sample = now
        # Keep the unthrottled observations across pauses. Discarding them
        # makes intermittent peers perpetually unknown. A paused peer gets
        # its ordinary weighted entitlement so new demand can wake it again.
        if self.paused:
            self.demand = None
            return
        self.demand_cpu += delta
        self.demand_elapsed += elapsed
        runnable = {p for p in self.members if p in table and table[p]['state'] == 'R'}
        self.runnable_since = {p: self.runnable_since.get(p, now) for p in runnable}
        pending = sum(now - since >= RUNNABLE_WINDOW for since in self.runnable_since.values())
        if self.demand_elapsed >= DEMAND_WINDOW:
            threads = runnable_threads(table, self.members)
            # Sustained runnable work protects CPU-starved peers, without
            # mistaking a brief wakeup for an entire core of demand. Check
            # non-leader threads less often to bound the inspection cost.
            self.observed_demand = None if threads is None else self.demand_cpu / self.demand_elapsed
            self.pending_threads = len((threads & self.previous_threads) - self.members) if threads is not None else 0
            self.previous_threads = threads if threads is not None else set()
            self.demand_elapsed = 0.
            self.demand_cpu = 0.
        self.demand = None if self.observed_demand is None else max(
            self.observed_demand, pending + self.pending_threads)

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
        self.update_demand(table, delta, now)
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

    def refill(self, rate, elapsed):
        # /proc CPU counters advance in whole clock ticks. Small shares
        # need enough saved credit to absorb a tick, including across idle
        # gaps between intermittent bursts. Never erase outstanding debt.
        self.credit = min(max(rate*BURST, ACCOUNTING_BURST), self.credit + rate*elapsed)

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
                total_weight = sum(j.config['weight'] for j in jobs.values())
                for key, job in list(jobs.items()):
                    try:
                        if job not in active:
                            idle_rate = len(cpus)*job.config['weight']/total_weight
                            if job.config.get('quota') is not None:
                                idle_rate = min(idle_rate, job.config['quota'])
                            job.refill(idle_rate, elapsed)
                            job.rate = 0
                            job.set_paused(False, table)
                            continue
                        rate = rates[job]
                        job.rate = rate
                        job.refill(rate, elapsed)
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
                                      'paused': j.paused, 'stops': j.stops,
                                      'demand_cpus': j.demand, 'rate_cpus': j.rate} for key,j in jobs.items()}}
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

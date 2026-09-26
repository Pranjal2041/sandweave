"""Unprivileged CPU-time sharing for explicitly registered lab process trees.

This is a sampled userspace controller, not a kernel cgroup. It uses host CPU
accounting and cooperative guest pause/resume intervals; quota overshoot is possible
within a sampling/discovery interval. Runtime and transport CPU is accounted,
but only guest execution is throttled; this is not a hard host CPU ceiling. The launcher itself is never stopped.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
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
import traceback

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
                'self_cpu': sum(map(int, fields[11:13])) / os.sysconf('SC_CLK_TCK'),
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
        from _unix_sockets import connect
        connect(conn, path)
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
    except (OSError, ConnectionError, RuntimeError, ValueError, KeyError):
        pass


class ControlConnection:
    """One in-flight request per sandbox; other sandboxes never wait on it."""
    def __init__(self, path):
        self.path = path
        self.socket = None

    def close(self):
        if self.socket is not None:
            self.socket.close()
            self.socket = None

    async def call(self, method, arg):
        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout(.5):
                if self.socket is None:
                    from _unix_sockets import Address
                    self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self.socket.setblocking(False)
                    with Address(self.path) as path:
                        await loop.sock_connect(self.socket, path)
                await loop.sock_sendall(self.socket, json.dumps({'method': 'containerManager.' + method,
                                                               'arg': arg}).encode())
                data = b''
                while len(data) < 65536:
                    part = await loop.sock_recv(self.socket, 4096)
                    if not part:
                        raise ConnectionError('CPU control socket closed')
                    data += part
                    try:
                        result = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not result['success']:
                        raise RuntimeError(result['err'])
                    return result.get('result')
                raise ValueError('CPU control reply exceeded limit')
        except BaseException:
            self.close()
            raise


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
        self.backend = 'runtime'
        self.desired_pause = False
        self.degraded = None
        self.last_sample = 0.
        self.helpers_cpu = 0.
        self.helper_samples = {}
        self.runnable_floor = 0
        self.runnable_time = 0.
        self.pending_pause = False

    def update_helpers(self, table, identities):
        samples = {}
        for pid, info in table.items():
            if info['start'] != identities[pid]:
                continue
            identity = (pid, info['start'])
            cpu = info['self_cpu']
            self.helpers_cpu += max(0., cpu - self.helper_samples.get(identity, cpu))
            samples[identity] = cpu
        self.helper_samples = samples

    def update_sample(self, sample, now):
        cpu = sample['CPUTimeNS'] / 1e9 + self.helpers_cpu
        delta = max(0., cpu - self.previous) if self.previous is not None else 0.
        self.previous = cpu
        self.total += delta
        self.credit -= delta
        elapsed = max(0., now - self.last_sample) if self.last_sample else 0.
        self.last_sample = now
        runnable = max(0, sample['Runnable'])
        if self.paused:
            self.demand = None
            self.runnable_floor = 0
            self.runnable_time = now
        else:
            self.demand_cpu += delta
            self.demand_elapsed += elapsed
            # A brief wakeup is not sustained demand. Keep a lower bound on
            # runnable tasks over an observation window, without per-task IO.
            self.runnable_floor = min(self.runnable_floor, runnable)
            if now - self.runnable_time >= RUNNABLE_WINDOW:
                self.pending_threads = self.runnable_floor
                self.runnable_floor = runnable
                self.runnable_time = now
            if self.demand_elapsed >= DEMAND_WINDOW:
                self.observed_demand = self.demand_cpu / self.demand_elapsed
                self.demand_cpu = self.demand_elapsed = 0.
            self.demand = None if self.observed_demand is None else max(
                self.observed_demand, self.pending_threads)
        if delta > .001 or runnable:
            self.active_until = now + .10
        self.paused = sample['Paused']

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

    def update(self, table, now, *, interval=TICK):
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
            self.active_until = now + max(.10, 2*interval)
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



async def release_control(connection):
    try:
        await connection.call('CPUControl', {'Disable': True})
    except RuntimeError as error:
        if 'unknown method' in str(error):
            await connection.call('SetCPUPaused', False)
        else:
            raise


async def serve_job(job, path, legacy_executor):
    connection = ControlConnection(job.config['control_path'])
    failed_since = None
    try:
        while path.exists() and job.degraded is None:
            started = time.monotonic()
            if not job.ready:
                await asyncio.sleep(TICK)
                continue
            try:
                if job.backend == 'legacy':
                    # Live snapshots pin older engines. Keep their policy,
                    # but isolate expensive compatibility sampling from the
                    # broker event loop and bound its worker count/cadence.
                    table = await asyncio.get_running_loop().run_in_executor(
                        legacy_executor, discover_trees, [job.config['root']])
                    if not job.update(table, time.monotonic(), interval=.25):
                        break
                    if not path.exists():
                        break
                    if job.desired_pause != job.paused:
                        job.pending_pause = job.desired_pause
                        await connection.call('SetCPUPaused', job.desired_pause)
                        job.paused = job.desired_pause
                        job.pending_pause = False
                        job.stops += int(job.paused)
                else:
                    job.pending_pause = job.desired_pause
                    sample = await connection.call('CPUControl', {'Paused': job.desired_pause})
                    if sample['Disabled']:
                        raise RuntimeError('CPU control disabled by launcher')
                    if sample['SampleAgeNS'] > 500_000_000:
                        raise RuntimeError('runtime CPU accounting stalled')
                    was_paused = job.paused
                    job.update_sample(sample, time.monotonic())
                    job.pending_pause = False
                    job.stops += int(job.paused and not was_paused)
                failed_since = None
            except (OSError, RuntimeError, ValueError, KeyError) as error:
                if 'unknown method' in str(error) and job.backend == 'runtime':
                    job.backend = 'legacy'
                    continue
                failed_since = failed_since or time.monotonic()
                job.desired_pause = False
                job.pending_pause = False
                job.demand = None
                if time.monotonic() - failed_since >= 3:
                    job.degraded = str(error) or type(error).__name__
                    path.with_name(path.name + '.failed').write_text(job.degraded + '\n')
                    break
            interval = .25 if job.backend == 'legacy' else TICK
            await asyncio.sleep(max(0., interval - (time.monotonic() - started)))
    finally:
        try:
            await release_control(connection)
        except (OSError, RuntimeError, ValueError, KeyError):
            pass
        connection.close()


async def broker_loop(directory, cpus):
    jobs, tasks = {}, set()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    previous = idle_since = time.monotonic()
    last_refresh = last_report = 0.
    suspended = set()
    legacy_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='legacy-cpu')
    def completed(task, job, path):
        tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            traceback.print_exception(error)
            job.degraded = str(error) or type(error).__name__
            try:
                path.with_name(path.name + '.failed').write_text(job.degraded + '\n')
            except OSError:
                pass
    try:
        while not stop.is_set():
            now = time.monotonic()
            elapsed = min(now - previous, .2)
            previous = now
            if now - last_refresh >= .25:
                paths = {p.name: p for p in directory.glob('job-*.json')}
                for key, path in paths.items():
                    if key in jobs:
                        continue
                    try:
                        cfg = json.loads(path.read_text())
                        if cfg['cpus'] != cpus or cfg['weight'] <= 0:
                            continue
                        job = jobs[key] = Job(cfg)
                        job.task = asyncio.create_task(serve_job(job, path, legacy_executor))
                        tasks.add(job.task)
                        job.task.add_done_callback(lambda task, job=job, path=path: completed(task, job, path))
                    except (OSError, ValueError, KeyError):
                        continue
                roots = process_table(j.config['root'] for j in jobs.values())
                suspended = {p.name.removesuffix('.suspend') for p in directory.glob('*.suspend')}
                for key, job in list(jobs.items()):
                    if key not in paths or roots.get(job.config['root'], {}).get('start') != job.config['start']:
                        job.task.cancel()
                        # Draining is asynchronous; never wait on a dead peer.
                        del jobs[key]
                        continue
                    if not job.ready:
                        try:
                            job.ready = json.loads(Path(job.config['state_path']).read_text())['status'] == 'running'
                        except (OSError, ValueError, KeyError):
                            pass
                    helpers = {job.config['root']: job.config['start']}
                    try:
                        record = json.loads(Path(job.config['helpers_path']).read_text())
                        helpers.update((p['pid'], p['start']) for p in record['processes'])
                    except (OSError, ValueError, KeyError):
                        pass
                    # Only explicitly owned launcher/transport PIDs. Never
                    # expand these into guest/stub descendants.
                    job.update_helpers(process_table(helpers), helpers)
                last_refresh = now
            if jobs:
                idle_since = now
            elif now - idle_since > 30:
                break
            eligible = {j for j in jobs.values() if j.degraded is None}
            active = [j for key,j in jobs.items() if j in eligible and j.ready and key not in suspended
                      and (j.paused or j.active_until > now)]
            rates = allocate_rates(active, len(cpus))
            total_weight = sum(j.config['weight'] for j in eligible)
            for key, job in jobs.items():
                if job.degraded is not None:
                    job.desired_pause = False
                    continue
                if job not in rates:
                    idle_rate = len(cpus)*job.config['weight']/total_weight
                    if job.config.get('quota') is not None:
                        idle_rate = min(idle_rate, job.config['quota'])
                    job.refill(idle_rate, elapsed)
                    job.rate = 0.
                    job.desired_pause = False
                else:
                    job.rate = rates[job]
                    job.refill(job.rate, elapsed)
                    job.desired_pause = job.credit < 0
            if now - last_report >= .25:
                report = {'pid': os.getpid(), 'time': time.time(), 'tick_seconds': TICK,
                          'cpus': cpus, 'jobs': {key: {
                              'cpu_seconds': j.total, 'credit': j.credit, 'paused': j.paused or j.pending_pause,
                              'stops': j.stops, 'demand_cpus': j.demand, 'rate_cpus': j.rate,
                              'accounting': j.backend, 'degraded': j.degraded,
                              'sample_age_seconds': now - j.last_sample if j.last_sample else None,
                          } for key,j in jobs.items()}}
                temp = directory / 'status.tmp'
                temp.write_text(json.dumps(report))
                temp.replace(directory / 'status.json')
                last_report = now
            await asyncio.sleep(max(0., TICK - (time.monotonic() - now)))
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        legacy_executor.shutdown(wait=False, cancel_futures=True)


def run(directory, cpus):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        asyncio.run(broker_loop(directory, cpus))


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
                               'weight': weight, 'quota': quota, 'helpers_path': str(Path(__file__).resolve().parent.parent / 'runs/gvisor' / name / 'owned-processes.json'), 'state_path': str(local / 'gvisor/state' / f'{name}_sandbox:{name}.state'), 'control_path': str(local / 'gvisor/state' / ('runsc-' + name + '.sock'))}))
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

"""Best-effort Linux measurements. Collection never controls admission or lifetime."""
import csv
import json
import os
from pathlib import Path
import subprocess
import threading
import time


def process_table(proc=Path('/proc')):
    result = {}
    for directory in proc.iterdir():
        if not directory.name.isdigit():
            continue
        try:
            if directory.stat().st_uid != os.getuid():
                continue
            fields = (directory / 'stat').read_text().rsplit(')', 1)[1].split()
            if fields[0] == 'Z':
                continue
            result[int(directory.name)] = dict(ppid=int(fields[1]), start=int(fields[19]),
                ticks=int(fields[11]) + int(fields[12]), rss=int(fields[21]) * os.sysconf('SC_PAGE_SIZE'))
        except (OSError, ValueError, IndexError):
            continue
    return result


def members(table, roots):
    """Require a matching PID and birth time before attributing any descendants."""
    children = {}
    for pid, info in table.items():
        children.setdefault(info['ppid'], []).append(pid)
    pending = [r['pid'] for r in roots if r and r.get('pid') in table and
               str(table[r['pid']]['start']) == str(r.get('start'))]
    found = set()
    while pending:
        pid = pending.pop()
        if pid not in found:
            found.add(pid)
            pending.extend(children.get(pid, ()))
    return found


def gpu_metrics(devices):
    if not devices:
        return [], None
    try:
        output = subprocess.check_output(['nvidia-smi', '-i', ','.join(g['uuid'] for g in devices),
            '--query-gpu=uuid,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw',
            '--format=csv,noheader,nounits'], text=True, stderr=subprocess.DEVNULL, timeout=2)
        def number(value, scale=1):
            try:
                return float(value.strip()) * scale
            except ValueError:
                return None
        return [dict(uuid=r[0].strip(), utilization_percent=number(r[1]),
                     memory_used_bytes=number(r[2], 1024**2), memory_total_bytes=number(r[3], 1024**2),
                     temperature_c=number(r[4]), power_watts=number(r[5]))
                for r in csv.reader(output.splitlines()) if len(r) == 6], None
    except (OSError, subprocess.SubprocessError) as error:
        return [], 'GPU measurements unavailable: ' + type(error).__name__


class Sampler:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.Lock()
        self.previous = None
        self.cached = None

    def sample(self, records, devices):
        with self.lock:
            if self.cached and time.monotonic() - self.cached['_clock'] < 5:
                return {k: v for k, v in self.cached.items() if k != '_clock'}
            try:
                result = self._sample(records, devices)
            except Exception as error:
                # A missing /proc, driver, or permission must not make a worker
                # unreachable or change reservations.
                result = {'time': time.time(), 'error': type(error).__name__ + ': ' + str(error),
                          'sandboxes': {}, 'gpus': []}
            self.cached = {**result, '_clock': time.monotonic()}
            return result

    def _sample(self, records, devices):
        now, clock = time.time(), time.monotonic()
        table = process_table()
        previous = self.previous
        elapsed = clock - previous['clock'] if previous else None
        ticks = os.sysconf('SC_CLK_TCK')
        sandboxes = {}
        attributed = set()
        for record in records:
            runtime = record.get('runtime_status', {})
            if runtime.get('status') not in ('running', 'paused', 'starting'):
                continue
            roots = [runtime.get('launcher'), runtime.get('sentry')]
            if record['spec']['runtime'] == 'apptainer':
                try:
                    roots.append(json.loads((Path(runtime['logs']) / 'native.json').read_text()))
                except (OSError, KeyError, ValueError):
                    pass
            pids = members(table, roots)
            attributed.update(pids)
            cpu = None
            if previous and pids:
                delta = sum(max(0, table[p]['ticks'] - previous['table'][p]['ticks']) for p in pids
                            if p in previous['table'] and table[p]['start'] == previous['table'][p]['start'])
                cpu = delta / ticks / elapsed
            sandboxes[record['id']] = {'cpu_cores': cpu,
                'rss_bytes': sum(table[p]['rss'] for p in pids) if pids else None,
                'processes': len(pids), 'time': now}
        cpus = set(os.sched_getaffinity(0))
        cpu_counters = {}
        for line in Path('/proc/stat').read_text().splitlines():
            name, *values = line.split()
            if name.startswith('cpu') and name[3:].isdigit() and int(name[3:]) in cpus:
                values = list(map(int, values))
                # Guest counters are already included in user/nice.
                cpu_counters[name] = (sum(values[:8]), values[3] + values[4])
        cpu_percent = None
        if previous and previous['cpus'].keys() == cpu_counters.keys():
            total = sum(v[0] - previous['cpus'][k][0] for k, v in cpu_counters.items())
            idle = sum(v[1] - previous['cpus'][k][1] for k, v in cpu_counters.items())
            if total > 0:
                cpu_percent = max(0, min(100, 100 * (total - idle) / total))
        memory = {k: int(v.strip().split()[0]) * 1024 for k, v in
                  (line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())}
        network = [list(map(int, line.split(':', 1)[1].split())) for line in
                   Path('/proc/net/dev').read_text().splitlines()[2:] if line.split(':')[0].strip() != 'lo']
        net = [sum(r[0] for r in network), sum(r[8] for r in network)]
        rates = [max(0, net[i] - previous['network'][i]) / elapsed if previous else None for i in range(2)]
        disk = os.statvfs(self.root)
        gpus, gpu_error = gpu_metrics(devices)
        self.previous = {'clock': clock, 'table': table, 'cpus': cpu_counters, 'network': net}
        return {'time': now, 'cpu_busy_percent': cpu_percent,
                'host_memory_total_bytes': memory['MemTotal'],
                'host_memory_available_bytes': memory.get('MemAvailable'),
                'sandbox_rss_bytes': sum(table[p]['rss'] for p in attributed),
                'network_receive_bytes_per_second': rates[0], 'network_send_bytes_per_second': rates[1],
                'disk_total_bytes': disk.f_blocks * disk.f_frsize,
                'disk_available_bytes': disk.f_bavail * disk.f_frsize,
                'gpus': gpus, 'gpu_error': gpu_error, 'sandboxes': sandboxes}


def tail(path, size=65536):
    """Read only a bounded suffix; never load an entire log into memory."""
    if type(size) is not int or not 1 <= size <= 262144:
        raise ValueError('log size must be between 1 and 262144 bytes')
    try:
        with Path(path).open('rb') as stream:
            stream.seek(0, 2)
            end = stream.tell()
            stream.seek(max(0, end - size))
            data = stream.read(size)
        return {'text': data.decode(errors='replace'), 'bytes': end, 'truncated': end > size}
    except FileNotFoundError:
        return {'text': '', 'bytes': 0, 'truncated': False, 'message': 'No log has been written yet.'}

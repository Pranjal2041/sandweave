"""Read-only cluster views and bounded measurement history, off the scheduler thread."""
from collections import Counter, defaultdict
import json
import logging
import math
import os
from pathlib import Path
import socket
import sqlite3
import threading
import time

from .scheduler import charged, requirements
from ..sandbox.telemetry import tail
from ..sandbox.wire import decode
from ..sandbox.proxy import public_resources

KINDS = ('workers', 'gpus', 'sandboxes', 'pools', 'jobs', 'tasks', 'snapshots')
LOG = logging.getLogger(__name__)


def pick(value, keys):
    return {k: value.get(k) for k in keys.split()}


def measured_total(samples, key):
    values = [sample[key] for sample in samples if sample.get(key) is not None]
    return sum(values) if values else None


def number(value, name, low, high, *, integer=False):
    try:
        result = int(value) if integer else float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError('invalid ' + name) from None
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f'{name} must be between {low} and {high}')
    return result


class Monitor:
    def __init__(self, controller, *, interval=5, history_hours=24, max_samples=200000):
        self.controller = controller
        self.interval = number(interval, 'monitor interval', 1, 300)
        self.retention = number(history_hours, 'history hours', 1, 168) * 3600
        self.max_samples = number(max_samples, 'maximum samples', 100, 2000000, integer=True)
        self.lock, self.db_lock = threading.RLock(), threading.RLock()
        self.stopping, self.thread = threading.Event(), None
        self.started = time.time()
        self.view = {'time': None, 'summary': {}, 'entities': {k: [] for k in KINDS}}
        self.error = None
        path = controller.state.root / 'monitor.sqlite'
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=5)
        os.chmod(path, 0o600)
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS samples (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL,
            kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS samples_entity ON samples(kind,id,time);
            CREATE INDEX IF NOT EXISTS samples_time ON samples(time);''')

    def start(self):
        self.thread = threading.Thread(target=self._loop, name='weave-monitor', daemon=True)
        self.thread.start()
        return self

    def _loop(self):
        while not self.stopping.is_set():
            try:
                self.refresh()
                self.error = None
            except Exception as error:
                self.error = type(error).__name__ + ': ' + str(error)
                LOG.exception('Monitoring collection failed; scheduling continues independently')
            self.stopping.wait(self.interval)

    def refresh(self):
        state = self.controller.state
        # Each read releases the authority lock before JSON decoding. Binary
        # programs and completed command output never enter the monitoring view.
        records = {k: state.metadata(k) for k in ('worker', 'allocation', 'pool', 'job', 'task', 'artifact', 'lease')}
        now = time.time()
        allocations, tasks = records['allocation'], records['task']
        workers, pools, jobs = {}, {}, {}
        entities = {k: [] for k in KINDS}
        usage = defaultdict(lambda: {'slots': 0, 'memory': 0, 'gpu': 0})
        for a in allocations:
            if charged(a):
                for k, v in requirements(a['spec']).items():
                    usage[a['worker']][k] += v
        for record in records['worker']:
            item = self.controller._public_worker(record)
            metric = dict(record.get('inventory', {}).get('telemetry', {}))
            # A prepared template may use another immutable worker workspace.
            # Its last measured data lives in the corresponding inventory.
            instances = [v.get('telemetry', {}) for v in record.get('instances', {}).values()]
            instances.append(metric)
            sandbox_metrics = {}
            for sample in instances:
                for identity, sample_value in sample.get('sandboxes', {}).items():
                    sample_value = {**sample_value, 'time': min(sample_value.get('time', 0), sample.get('time', 0))}
                    if sample_value.get('time', 0) >= sandbox_metrics.get(identity, {}).get('time', 0):
                        sandbox_metrics[identity] = sample_value
            metric['sandboxes'] = sandbox_metrics
            metric['sandbox_rss_bytes'] = measured_total((m for m in sandbox_metrics.values()
                if now - m.get('time', 0) <= max(20, self.interval * 3)), 'rss_bytes') if sandbox_metrics else metric.get('sandbox_rss_bytes')
            item.update(hostname=record.get('inventory', {}).get('hostname'),
                        reserved=dict(usage[record['id']]), telemetry=metric,
                        telemetry_stale=now - metric.get('time', 0) > max(20, self.interval * 3))
            workers[item['id']] = item
            entities['workers'].append(item)
            for gpu in item.get('gpus') or []:
                measurement = next((v for v in metric.get('gpus', []) if v['uuid'] == gpu['uuid']), {})
                entities['gpus'].append(dict(id=item['id'] + ':' + gpu['uuid'], worker=item['id'],
                    name=gpu.get('model'), uuid=gpu['uuid'], state='removed' if item['state'] == 'removed' else
                    'unavailable' if item['telemetry_stale'] or not measurement else 'ready',
                    telemetry={} if item['telemetry_stale'] else measurement, updated=metric.get('time')))
        by_pool = defaultdict(list)
        for a in allocations:
            spec = a['spec']
            info = a.get('info') or {}
            worker = workers.get(a.get('worker'), {})
            telemetry = worker.get('telemetry', {}).get('sandboxes', {}).get(a['id'], {})
            stale = now - telemetry.get('time', 0) > max(20, self.interval * 3)
            metric = telemetry if not stale else {}
            item = self.controller._public_allocation(a)
            # Never send the worker's full describe record: it contains command
            # agent credentials, mount paths, and setup scripts.
            item.pop('info', None)
            item.update(name=spec.get('name'), template=spec['template']['name'], runtime=spec['runtime'],
                        resources=public_resources(spec['resources']), desired=a.get('desired'),
                        detached=spec.get('detached'), role=a.get('role'),
                        leased=bool(a.get('lease')), timings=info.get('timings', {}),
                        telemetry=metric, telemetry_stale=stale)
            entities['sandboxes'].append(item)
            by_pool[a.get('parent')].append(item)
        leases = defaultdict(list)
        for lease in records['lease']:
            leases[lease.get('parent')].append(lease)
        for p in records['pool']:
            live = [a for a in by_pool[p['id']] if not a.get('released')]
            item = pick(p, 'id name state size warm weight priority labels placement error created updated')
            item.update(template=p['request']['spec']['template']['name'],
                ready=sum(a['state'] == 'ready' and not a['leased'] and a['role'] != 'builder' and a['desired'] == 'running' for a in live),
                active=sum(a['leased'] for a in live),
                pending=sum(a['state'] in ('pending', 'reserved', 'starting', 'unknown') for a in live),
                waiting=sum(l['state'] == 'pending' for l in leases[p['id']]),
                reason=next((a.get('reason') or a.get('error') for a in live if a.get('reason') or a.get('error')), None),
                cpu_cores=measured_total((a['telemetry'] for a in live), 'cpu_cores'),
                rss_bytes=measured_total((a['telemetry'] for a in live), 'rss_bytes'),
                measured_sandboxes=sum(a['telemetry'].get('rss_bytes') is not None for a in live))
            pools[p['id']] = item
            entities['pools'].append(item)
        job_tasks = defaultdict(list)
        allocation_workers = {a['id']: a.get('worker') for a in allocations}
        for t in tasks:
            item = pick(t, 'id parent index state attempt sandbox error created updated')
            item.update(returncode=t.get('result', {}).get('returncode'),
                        worker=allocation_workers.get(t.get('sandbox')))
            job_tasks[t['parent']].append(item)
            entities['tasks'].append(item)
        for j in records['job']:
            item = pick(j, 'id state pool created updated error')
            jt = job_tasks[j['id']]
            item.update(tasks=len(jt), succeeded=sum(t['state'] == 'succeeded' for t in jt),
                        failed=sum(t['state'] == 'failed' for t in jt),
                        attempts=sum(t['attempt'] + 1 for t in jt),
                        template=pools.get(j['pool'], {}).get('template'))
            jobs[j['id']] = item
            entities['jobs'].append(item)
        for a in records['artifact']:
            item = pick(a, 'id created updated')
            reference = a.get('info', {})
            item.update(state='registered', replicas=len(a.get('locations', [])),
                        kind=reference.get('state') or reference.get('kind'), source=reference.get('source'),
                        template=reference.get('template'))
            entities['snapshots'].append(item)
        active_workers = [w for w in workers.values() if w['state'] != 'removed']
        fresh = [w for w in active_workers if not w['telemetry_stale']]
        live = [a for a in entities['sandboxes'] if not a.get('released')]
        measured = [a for a in live if a['telemetry'].get('rss_bytes') is not None]
        cpus = sum(w['capacity']['cpus'] for w in fresh if w['telemetry'].get('cpu_busy_percent') is not None)
        summary = dict(workers=len(active_workers), ready_workers=sum(w['state'] == 'ready' for w in active_workers),
            draining_workers=sum(bool(w.get('draining')) for w in active_workers),
            stale_workers=sum(w['telemetry_stale'] for w in active_workers),
            capacity={k: sum(w['capacity'].get(k, 0) for w in active_workers) for k in ('slots', 'cpus', 'memory', 'gpu')},
            reserved={k: sum(w['reserved'].get(k, 0) + (w.get('external') or {}).get(k, 0) for w in active_workers) for k in ('slots', 'memory', 'gpu')},
            sandboxes=len(live), sandbox_states=dict(Counter(a['state'] for a in live)),
            pools=sum(p['state'] != 'closed' for p in pools.values()),
            jobs=dict(Counter(j['state'] for j in jobs.values())), tasks=dict(Counter(t['state'] for t in entities['tasks'])),
            cpu_busy_percent=sum(w['telemetry'].get('cpu_busy_percent', 0) * w['capacity']['cpus'] for w in fresh
                                 if w['telemetry'].get('cpu_busy_percent') is not None) / cpus if cpus else None,
            cpu_cores=measured_total((a['telemetry'] for a in live), 'cpu_cores'),
            rss_bytes=measured_total((a['telemetry'] for a in live), 'rss_bytes'),
            measured_sandboxes=len(measured),
            pending=sum(a['state'] == 'pending' for a in live),
            unknown=sum(a['state'] == 'unknown' for a in live))
        timings = sorted(a['timings']['ready_seconds'] for a in entities['sandboxes'] if
                         isinstance(a['timings'].get('ready_seconds'), (int, float)) and a.get('created', 0) > now - 3600)
        summary['startup'] = {'count': len(timings), 'p50': timings[(len(timings)-1)//2] if timings else None,
                              'p95': timings[max(0, math.ceil(len(timings)*.95)-1)] if timings else None}
        alerts = []
        for w in active_workers:
            if w['state'] != 'ready' or w['telemetry_stale']:
                alerts.append(dict(kind='workers', id=w['id'], severity='error' if w['state'] != 'ready' else 'warning',
                    message=w.get('error') or 'Worker measurements are stale or unavailable.'))
            disk = w['telemetry']
            if not w['telemetry_stale'] and disk.get('disk_total_bytes') and disk['disk_available_bytes'] / disk['disk_total_bytes'] < .1:
                alerts.append(dict(kind='workers', id=w['id'], severity='warning', message='Storage has less than 10% available.'))
        for a in live:
            if a.get('error') or a['state'] == 'pending' and now - a['created'] > 30:
                alerts.append(dict(kind='sandboxes', id=a['id'], severity='warning', message=a.get('error') or a.get('reason') or 'Waiting for placement.'))
        view = {'time': now, 'summary': summary, 'entities': entities, 'alerts': alerts[:100],
                'alert_count': len(alerts)}
        samples = [('cluster', 'cluster', {**{k: summary[k] for k in ('cpu_busy_percent', 'cpu_cores', 'rss_bytes', 'sandboxes', 'pending', 'unknown')},
                                            'reserved_memory': summary['reserved']['memory']})]
        for w in fresh:
            metric = w['telemetry']
            samples.append(('workers', w['id'], {**pick(metric, 'cpu_busy_percent sandbox_rss_bytes disk_available_bytes network_receive_bytes_per_second network_send_bytes_per_second'),
                                                 'reserved_memory': w['reserved']['memory']}))
        for a in measured:
            samples.append(('sandboxes', a['id'], pick(a['telemetry'], 'cpu_cores rss_bytes')))
        for p in entities['pools']:
            if p['state'] != 'closed':
                samples.append(('pools', p['id'], pick(p, 'ready active pending waiting cpu_cores rss_bytes')))
        for gpu in entities['gpus']:
            if gpu['state'] == 'ready':
                samples.append(('gpus', gpu['id'], pick(gpu['telemetry'], 'utilization_percent memory_used_bytes temperature_c power_watts')))
        # Keep aggregate history when a very large entity batch hits the global cap.
        samples.append(samples.pop(0))
        with self.db_lock:
            self.db.execute('BEGIN')
            try:
                self.db.executemany('INSERT INTO samples(time,kind,id,data) VALUES (?,?,?,?)',
                    [(now, kind, identity, json.dumps(data, allow_nan=False)) for kind, identity, data in samples])
                self.db.execute('DELETE FROM samples WHERE time < ?', (now - self.retention,))
                cutoff = self.db.execute('SELECT sequence FROM samples ORDER BY sequence DESC LIMIT 1 OFFSET ?',
                                         (self.max_samples - 1,)).fetchone()
                if cutoff:
                    self.db.execute('DELETE FROM samples WHERE sequence < ?', cutoff)
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
        with self.lock:
            self.view = view

    def overview(self):
        with self.controller.guard:
            pending = sum(not f.done() for f in self.controller.pending.values())
        with self.controller.relay.condition:
            forwarding = len(self.controller.relay.pending)
        try:
            rss = int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
        except (OSError, IndexError, ValueError):
            rss = None
        with self.lock:
            view = self.view
            return {k: v for k, v in view.items() if k != 'entities'} | {
                'cluster_id': self.controller.id, 'interval': self.interval, 'history_hours': self.retention / 3600,
                'max_samples': self.max_samples, 'error': self.error,
                'uptime_seconds': time.time() - self.started,
                'last_reconcile': getattr(self.controller, 'last_tick', None),
                'reconcile_error': getattr(self.controller, 'last_error', None),
                'controller': dict(hostname=socket.gethostname(), pid=os.getpid(), rss_bytes=rss,
                    threads=threading.active_count(), pending_operations=pending, forwarding_requests=forwarding),
                'counts': {k: len(v) for k, v in view['entities'].items()}}

    def listing(self, kind, *, q='', state='', worker='', pool='', template='', offset=0, limit=50, sort='updated', direction='desc'):
        if kind not in KINDS:
            raise ValueError('unknown resource view')
        offset = number(offset, 'offset', 0, 10000000, integer=True)
        limit = number(limit, 'limit', 1, 200, integer=True)
        if sort not in ('updated', 'created', 'name', 'state', 'id') or direction not in ('asc', 'desc'):
            raise ValueError('invalid sort')
        with self.lock:
            values = list(self.view['entities'][kind])
        query = str(q).casefold()[:200]
        values = [v for v in values if (not state or v.get('state') == state) and
                  (not worker or v.get('worker') == worker or v['id'] == worker) and
                  (not pool or v.get('parent') == pool or v.get('pool') == pool or v['id'] == pool) and
                  (not template or v.get('template') == template) and
                  (not query or query in json.dumps(pick(v, 'id name state hostname template worker parent pool labels error reason')).casefold())]
        values.sort(key=lambda v: (v.get(sort) or (0 if sort in ('updated', 'created') else ''), v['id']), reverse=direction == 'desc')
        return {'items': values[offset:offset+limit], 'total': len(values), 'offset': offset, 'limit': limit}

    def detail(self, kind, identity):
        if kind not in KINDS:
            raise ValueError('unknown resource view')
        with self.lock:
            value = next((v for v in self.view['entities'][kind] if v['id'] == identity), None)
        if value is None:
            raise FileNotFoundError('resource is not in the latest monitoring sample')
        result = dict(value)
        if kind == 'tasks':
            result['attempts'] = [dict(id=a['id'], time=a['created'],
                **pick(a.get('result', {}), 'returncode timed_out output_limited failure error'))
                for a in reversed(self.controller.state.metadata('attempt', parent=identity, limit=100))]
        return result

    def history(self, kind='cluster', identity='cluster', seconds=3600):
        if kind not in ('cluster', *KINDS):
            raise ValueError('unknown history view')
        seconds = number(seconds, 'history seconds', 60, 604800)
        since = time.time() - seconds
        bucket = max(self.interval, seconds / 180)
        # SQL bounds returned points, even when a long range has many samples.
        with self.db_lock:
            rows = self.db.execute('SELECT time,data FROM samples WHERE sequence IN '
                '(SELECT MAX(sequence) FROM samples WHERE kind=? AND id=? AND time>=? '
                'GROUP BY CAST((time-?)/? AS INTEGER)) ORDER BY time',
                (kind, identity, since, since, bucket)).fetchall()
            extent = self.db.execute('SELECT MIN(time),MAX(time) FROM samples WHERE kind=? AND id=?', (kind, identity)).fetchone()
        return {'points': [dict(time=t, **json.loads(d)) for t, d in rows], 'bucket_seconds': bucket,
                'earliest': extent[0], 'latest': extent[1], 'method': 'last sample per bucket'}

    def events(self, *, before=0, after=0, kind='', identity='', q='', limit=50):
        before = number(before, 'before', 0, 2**63-1, integer=True)
        after = number(after, 'after', 0, 2**63-1, integer=True)
        limit = number(limit, 'limit', 1, 200, integer=True)
        clauses, values = [], []
        for column, op, val in (('sequence', '<', before), ('sequence', '>', after), ('kind', '=', kind), ('id', '=', identity)):
            if val:
                clauses.append(column + op + '?'); values.append(val)
        if q:
            # The event payload is a framed binary blob; search identifiers in
            # SQL and filter message text only within the bounded returned page.
            clauses.append('(id LIKE ? OR kind LIKE ?)')
            values.extend(['%' + str(q)[:200] + '%'] * 2)
        sql = 'SELECT sequence,time,kind,id,data FROM events'
        with self.controller.state.lock:
            rows = self.controller.state.db.execute(sql + (' WHERE ' + ' AND '.join(clauses) if clauses else '') +
                ' ORDER BY sequence DESC LIMIT ?', [*values, limit + 1]).fetchall()
        return {'items': [dict(sequence=r[0], time=r[1], kind=r[2], id=r[3], detail=decode(bytes(r[4]))) for r in rows[:limit]],
                'more': len(rows) > limit}

    def logs(self, kind='controller', identity='', stream='stdout', attempt='', size=65536):
        size = number(size, 'log bytes', 1, 262144, integer=True)
        if kind == 'controller':
            return tail(self.controller.state.root / 'controller.log', size)
        if kind == 'sandboxes':
            a = self.controller._resolve(identity)
            if not a.get('endpoint'):
                return {'text': '', 'message': 'Waiting for a worker assignment.'}
            connection = self.controller.connection(a['endpoint'], timeout=5)
            try:
                return connection.call('monitor_logs', identity=a['id'], stream=stream, size=size)
            finally:
                connection.close()
        if kind != 'tasks' or stream not in ('stdout', 'stderr'):
            raise ValueError('choose a task stdout/stderr or a sandbox launcher/runtime log')
        task = self.controller.state.get('task', identity)
        result = task.get('result')
        if attempt:
            record = self.controller.state.get('attempt', attempt)
            if record.get('parent') != identity:
                raise ValueError('attempt belongs to a different task')
            result = record['result']
        if result is not None:
            data = result.get(stream, b'')
            return dict(text=data[-size:].decode(errors='replace'), bytes=len(data), truncated=len(data) > size,
                        returncode=result.get('returncode'), finished=True)
        if not task.get('sandbox') or not task.get('process') or task['state'] not in ('running', 'starting'):
            return {'text': '', 'message': 'This task has no running command or saved output yet.'}
        route = self.controller.allocation_route(task['sandbox'])
        connection = self.controller.connection(route['endpoint'], timeout=5)
        try:
            status = connection.call('process_status', identity=task['sandbox'], process_id=task['process'])
            total = status.get(stream + '_size', 0)
            data = connection.call('process_output', identity=task['sandbox'], process_id=task['process'],
                                   stream=stream, offset=max(0, total-size), size=size)
            return dict(text=data.decode(errors='replace'), bytes=total, truncated=total > size,
                        returncode=status.get('returncode'), finished=status.get('returncode') is not None)
        finally:
            connection.close()

    def prometheus(self):
        with self.lock:
            view = self.view
        lines = ['# HELP sandweave_monitor_sample_timestamp_seconds Last successful controller sample.',
                 '# TYPE sandweave_monitor_sample_timestamp_seconds gauge',
                 'sandweave_monitor_sample_timestamp_seconds ' + str(view['time'] or 0)]
        def metric(name, help_text, rows):
            lines.extend(['# HELP ' + name + ' ' + help_text, '# TYPE ' + name + ' gauge'])
            for labels, value in rows:
                if isinstance(value, (int, float)) and math.isfinite(value):
                    label = ','.join(k + '=' + json.dumps(str(v), ensure_ascii=True) for k, v in labels.items())
                    lines.append(name + ('{' + label + '}' if label else '') + ' ' + str(float(value)))
        workers = view['entities']['workers']
        metric('sandweave_worker_ready', 'Worker has acknowledged contact.',
               [({'worker': w['id']}, int(w['state'] == 'ready')) for w in workers if w['state'] != 'removed'])
        metric('sandweave_worker_sample_timestamp_seconds', 'Worker measurement timestamp.',
               [({'worker': w['id']}, w['telemetry'].get('time')) for w in workers if w['state'] != 'removed'])
        for resource in ('slots', 'cpus', 'memory', 'gpu'):
            metric('sandweave_capacity_' + resource, 'Registered worker capacity; memory is in bytes.',
                   [({'worker': w['id']}, w['capacity'].get(resource)) for w in workers if w['state'] != 'removed'])
        for resource in ('slots', 'memory', 'gpu'):
            metric('sandweave_reserved_' + resource, 'Reserved capacity including external sandboxes; memory is in bytes.',
                   [({'worker': w['id']}, w['reserved'].get(resource, 0) + (w.get('external') or {}).get(resource, 0)) for w in workers if w['state'] != 'removed'])
        for key in ('cpu_busy_percent', 'sandbox_rss_bytes', 'disk_available_bytes', 'network_receive_bytes_per_second', 'network_send_bytes_per_second'):
            metric('sandweave_worker_' + key, 'Measured worker value; host network and disk include other workloads.',
                   [({'worker': w['id']}, w['telemetry'].get(key)) for w in workers if not w['telemetry_stale'] and w['state'] != 'removed'])
        for key in ('utilization_percent', 'memory_used_bytes', 'memory_total_bytes', 'temperature_c', 'power_watts'):
            metric('sandweave_gpu_' + key, 'Device measurement across all processes on the eligible GPU.',
                   [({'worker': w['id'], 'uuid': g['uuid']}, g.get(key)) for w in workers if not w['telemetry_stale'] and w['state'] != 'removed' for g in w['telemetry'].get('gpus', [])])
        for kind in ('sandboxes', 'jobs', 'tasks'):
            counts = Counter(v['state'] for v in view['entities'][kind])
            metric('sandweave_' + kind, 'Controller records by state, including retained completed records.',
                   [({'state': k}, v) for k, v in counts.items()])
        return '\n'.join(lines) + '\n'

    def close(self):
        self.stopping.set()
        if self.thread:
            self.thread.join()
        with self.db_lock:
            self.db.close()

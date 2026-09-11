"""Durable desired state reconciled against independently running workers."""
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import logging
import threading
import time
import uuid

from . import PROTOCOL, providers, scheduler
from .state import State
from ..sandbox.errors import OperationUnknown, ResourceUnavailable, OwnerExpired
from ..sandbox.ownership import Owners, GRACE_SECONDS
from ..sandbox.resources import positive, memory_bytes
from ..sandbox.wire import encode

LOG = logging.getLogger(__name__)


class Controller:
    def __init__(self, directory, *, connector=None, workers=16, interval=.1):
        self.state = State(directory)
        settings = self.state.get('settings', 'cluster', required=False)
        if settings is None:
            settings = self.state.put('settings', {'id': 'cluster', 'cluster_id': uuid.uuid4().hex})
        self.id = settings['cluster_id']
        self.connector = connector or providers.attach
        from .relay import Broker
        self.relay = Broker()
        self.owners = Owners(self.state.root)
        # A controller outage is not owner death. Give surviving remote clients
        # time to renew after restart; previously decided expiry stays final.
        for path in self.owners.root.glob('*.json'):
            owner = json.loads(path.read_text())
            if not owner.get('reason'):
                self.owners._write({**owner, 'expires_at': time.time() + GRACE_SECONDS})
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='weave-worker')
        self.pending, self.guard = {}, threading.RLock()
        self.stopping, self.interval = threading.Event(), interval
        self.thread = None
        self.next_probe = 0
        self.last_tick, self.last_error = None, None

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, name='weave-controller', daemon=True)
            self.thread.start()
        return self

    def connection(self, endpoint, **options):
        if endpoint.get('relay'):
            from .relay import RelayConnection
            return RelayConnection(self.relay, endpoint, **options)
        return providers.direct(endpoint, **options)

    def _attach(self, target, **options):
        if isinstance(target, dict) and target.get('endpoint', {}).get('relay'):
            return self.connection(target['endpoint'])
        return self.connector(target, **options)

    def sandbox_rpc(self, identity, method, parameters):
        # Resolve stored routes, never accept an arbitrary destination from a
        # client. The worker still validates the sandbox-scoped credential.
        route = self.allocation_route(identity)
        connection = self.connection(route['endpoint'])
        try:
            return connection.call(method, **parameters)
        finally:
            connection.close()

    def _loop(self):
        while not self.stopping.is_set():
            try:
                self.tick()
                self.last_tick, self.last_error = time.time(), None
            except Exception as error:
                self.last_error = str(error)
                LOG.exception('Reconciliation failed; will retry')
            self.stopping.wait(self.interval)

    def _submit(self, key, function, *args):
        with self.guard:
            previous = self.pending.get(key)
            if previous is not None and not previous.done():
                return
            if previous is not None:
                try:
                    previous.result()
                except Exception:
                    LOG.exception('Worker operation failed: %s', key)
            if len([f for f in self.pending.values() if not f.done()]) >= self.executor._max_workers * 2:
                return
            self.pending[key] = self.executor.submit(function, *args)

    def owner_register(self, identity, process):
        return self.owners.register(identity, process)

    def owner_heartbeat(self, identity):
        result = self.owners.heartbeat(identity)
        result['routes'] = [self.allocation_route(a['id']) for a in self.state.list('allocation')
                            if a.get('owner') == identity and a.get('token') and not a.get('released')]
        return result

    def owner_routes(self, identity, sandboxes):
        self._owner_process(identity)
        with self.state.transaction():
            for sandbox in sandboxes:
                record = self.state.get('allocation', sandbox)
                if record.get('owner') != identity:
                    raise PermissionError('sandbox belongs to another process owner')
                if record.get('owner_route_ack') != identity:
                    self.state.put('allocation', {**record, 'owner_route_ack': identity})
        return {'acknowledged': True}

    def _owner_process(self, identity):
        if identity is None:
            return None
        if self.owners.reason(identity):
            raise OwnerExpired('creator is no longer active')
        with self.owners.lock:
            return self.owners._read(identity)['process']

    def worker_add(self, target='local', *, slots=None, memory=None, labels=None, name=None):
        target = providers.serialize(target)
        from ..templates.resolve import Template
        connection = self._attach(target, template=Template('coding').resolve())
        try:
            info = connection.call('inventory')
            ping = connection.call('ping')
            route = providers.endpoint(ping, connection, target)
        finally:
            connection.close()
        if info.get('protocol') != PROTOCOL:
            raise ResourceUnavailable('worker protocol differs; install the same Sandweave version on the worker')
        slots = len(info['cpus']) if slots is None else positive(slots, 'slots', integer=True)
        memory = info['memory'] if memory is None else memory_bytes(memory)
        if memory > info['memory']:
            raise ValueError('worker memory reservation exceeds its eligible budget')
        if labels is not None and (not isinstance(labels, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in labels.items())):
            raise ValueError('worker labels must map strings to strings')
        identity = 'worker-' + hashlib.sha256(encode([info['hostname'], info['workspace']])).hexdigest()[:16]
        with self.state.transaction():
            previous = self.state.get('worker', identity, required=False)
            if previous and previous.get('lost'):
                raise ResourceUnavailable('worker was declared lost; join with a new worker workspace after stopping the old allocation')
            if previous and previous['state'] != 'removed':
                if (previous['capacity']['slots'] != slots or previous['capacity']['memory'] != memory or
                        previous.get('labels', {}) != (labels or {})):
                    raise ValueError('worker is already registered with different limits or labels; drain and remove it before rejoining')
                self.state.put('worker', {**previous, 'target': target})
                self._accept_worker(identity, connection, info, ping=ping)
                # Retain draining and every outstanding assignment on retry.
                return self._public_worker(self.state.get('worker', identity))
            for other in self.state.list('worker'):
                if other['id'] == identity or other['state'] == 'removed':
                    continue
                if other['inventory']['scope'] == info['scope']:
                    if set(other['inventory']['cpus']) & set(info['cpus']):
                        raise ValueError('worker CPU allocation overlaps registered worker ' + other['id'])
                    if {g['uuid'] for g in other.get('gpus', [])} & {g['uuid'] for g in info['gpus']}:
                        raise ValueError('worker GPU allocation overlaps registered worker ' + other['id'])
            record = self.state.put('worker', dict(id=identity, state='ready', target=target,
                machine=scheduler.machine(info),
                name=name or info['hostname'], endpoint=route, labels=labels or {}, inventory=info,
                capacity={'slots': slots, 'cpus': len(info['cpus']), 'memory': memory, 'gpu': len(info['gpus'])},
                cpu_ids=info['cpus'],
                gpus=info['gpus'], runtimes=info['runtimes'], draining=False, seen=time.time(),
                instances={info['workspace']: {'endpoint': route, 'external': self._external(info),
                                             'telemetry': info.get('telemetry', {})}},
                external=self._external(info)), event={'message': 'worker registered'})
        return self._public_worker(record)

    def _external(self, inventory):
        live = [r for r in inventory['live'] if r.get('cluster') != self.id]
        return {'memory': sum(r['memory'] for r in live), 'slots': len(live),
                'gpu': sum(int(r['gpu']) for r in live),
                'gpu_uuids': sorted({g for r in live for g in r.get('gpu_uuids', [])})}

    @staticmethod
    def _sum_external(record):
        values = [v['external'] for v in record['instances'].values()]
        return {**{k: sum(v[k] for v in values) for k in ('memory', 'slots', 'gpu')},
                'gpu_uuids': sorted({g for v in values for g in v.get('gpu_uuids', [])})}

    @staticmethod
    def _public_worker(record):
        return {k: record.get(k) for k in ('id', 'name', 'state', 'labels', 'capacity', 'external',
                                         'cpu_ids', 'gpus', 'machine', 'draining', 'seen', 'error')}

    def worker_list(self):
        return [self._public_worker(w) for w in self.state.list('worker')]

    def worker_update(self, identity, *, draining=None, slots=None, labels=None, remove=False, lost=False):
        if type(lost) is not bool or (lost and not remove):
            raise ValueError('lost=True is only valid when removing a worker whose allocation has stopped')
        with self.state.transaction():
            worker = self.state.get('worker', identity)
            if remove and not lost and any(scheduler.charged(a) for a in self.state.list('allocation', worker=identity)):
                raise ResourceUnavailable('worker still has reserved sandboxes; wait for cleanup, or use lost=True only after confirming its allocation has stopped')
            if draining is not None:
                if type(draining) is not bool:
                    raise ValueError('draining must be a bool')
                worker['draining'] = draining
            if slots is not None:
                worker['capacity']['slots'] = positive(slots, 'slots', integer=True)
            if labels is not None:
                if not isinstance(labels, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in labels.items()):
                    raise ValueError('labels must map strings to strings')
                worker['labels'] = labels
            if remove:
                worker['state'] = 'removed'
                if lost:
                    worker.update(lost=True, draining=True)
                    error = 'worker allocation was confirmed stopped by the operator'
                    for allocation in self.state.list('allocation', worker=identity):
                        if not scheduler.charged(allocation):
                            continue
                        self.state.put('allocation', {**allocation, 'state': 'failed', 'desired': 'terminated',
                            'released': True, 'generation': allocation['generation'] + 1, 'error': error},
                            event={'message': error})
                        if allocation.get('lease'):
                            lease = self.state.get('lease', allocation['lease'])
                            if lease['state'] in ('pending', 'claiming', 'ready'):
                                self.state.put('lease', {**lease, 'state': 'failed', 'error': error})
            result = self.state.put('worker', worker, event={'message': 'worker configuration updated'})
        return self._public_worker(result)

    def _probe(self, identity):
        record = self.state.get('worker', identity)
        if record['state'] == 'removed':
            return
        connection = None
        try:
            try:
                connection = self.connection(record['endpoint'], timeout=5)
                info = connection.call('inventory')
            except Exception:
                if connection:
                    connection.close()
                if record['endpoint'].get('relay'):
                    raise  # Outbound agents reconnect themselves; no inbound fallback.
                # Existing targets check exact process ownership before they
                # restart a dead worker. Opaque endpoints are only reconnectable.
                connection = self._attach(record['target'])
                info = connection.call('inventory')
                if info['workspace'] != record['inventory']['workspace']:
                    raise ResourceUnavailable('worker workspace changed while disconnected; existing assignments remain reserved')
            self._accept_worker(identity, connection, info)
            # Older installations may still contain unrelated local sandboxes.
            # Keep their last reservation if that authority is unavailable.
            record = self.state.get('worker', identity)
            for workspace, instance in record.get('instances', {}).items():
                if workspace == info['workspace']:
                    continue
                previous = self.connection(instance['endpoint'], timeout=5)
                try:
                    old_info = previous.call('inventory')
                    with self.state.transaction():
                        current = self.state.get('worker', identity)
                        if current['state'] == 'removed':
                            return
                        current['instances'][workspace]['external'] = self._external(old_info)
                        current['instances'][workspace]['telemetry'] = old_info.get('telemetry', {})
                        self.state.put('worker', current)
                except Exception:
                    pass
                finally:
                    previous.close()
            with self.state.transaction():
                record = self.state.get('worker', identity)
                if record['state'] == 'removed':
                    return
                recovered = record['state'] != 'ready'
                record.update(state='ready', seen=time.time(), external=self._sum_external(record))
                record.pop('error', None)
                self.state.put('worker', record, event={'message': 'worker reachable'} if recovered else None)
        except Exception as error:
            with self.state.transaction():
                record = self.state.get('worker', identity)
                if record['state'] != 'removed':
                    self.state.put('worker', {**record, 'state': 'unreachable', 'error': str(error)},
                                   event={'message': 'worker unreachable', 'error': str(error)} if record['state'] != 'unreachable' else None)
        finally:
            if connection:
                connection.close()

    def _accept_worker(self, identity, connection, information=None, *, promote=False, ping=None):
        info = information or connection.call('inventory')
        ping = ping or connection.call('ping')
        with self.state.transaction():
            record = self.state.get('worker', identity)
            if record['state'] == 'removed':
                raise ResourceUnavailable('worker has been removed')
            old = record['inventory']
            if (info['protocol'] != PROTOCOL or old['scope'] != info['scope'] or
                    set(old['cpus']) != set(info['cpus']) or
                    {g['uuid'] for g in old['gpus']} != {g['uuid'] for g in info['gpus']}):
                raise ResourceUnavailable('worker resource allocation changed; drain and register it separately')
            route = providers.endpoint(ping, connection, record['target'])
            record.setdefault('instances', {})[info['workspace']] = {
                'endpoint': route, 'external': self._external(info), 'telemetry': info.get('telemetry', {})}
            if promote or old['workspace'] == info['workspace']:
                record.update(endpoint=route, inventory=info)
            record['external'] = self._sum_external(record)
            record['machine'] = scheduler.machine(info)
            self.state.put('worker', record)
            for allocation in self.state.list('allocation', worker=identity):
                if (allocation.get('endpoint', {}).get('workspace') == info['workspace'] and
                        allocation['endpoint'] != route):
                    self.state.put('allocation', {**allocation, 'endpoint': route})
            for artifact in self.state.list('artifact'):
                locations = [route if location.get('hostname') == info['hostname'] and
                             location.get('workspace') == info['workspace'] else location
                             for location in artifact['locations']]
                if locations != artifact['locations']:
                    self.state.put('artifact', {**artifact, 'locations': locations})
        return route

    def create(self, identity, spec, *, operation_id=None, reference=None, cache_key=None,
               refresh=False, owner=None):
        import re
        if not isinstance(identity, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', identity):
            raise ValueError('invalid sandbox ID')
        positive(spec['startup_timeout'], 'startup_timeout')
        self._owner_process(owner)
        if not spec.get('detached') and owner is None:
            raise ValueError('an attached sandbox needs a process owner')
        request = dict(spec=spec, operation_id=operation_id, reference=reference, cache_key=cache_key,
                       refresh=refresh, owner=owner)
        stamp = hashlib.sha256(encode(request)).hexdigest()
        canonical = hashlib.sha256(encode(request, canonical=True)).hexdigest()
        with self.state.transaction():
            old = self.state.get('allocation', identity, required=False)
            if old:
                if (old['canonical_stamp'] != canonical if 'canonical_stamp' in old else old['stamp'] != stamp):
                    raise FileExistsError('sandbox ID belongs to a different request')
                return self._public_allocation(old)
            if spec.get('name') and any(a['spec'].get('name') == spec['name'] and not a.get('released')
                                       for a in self.state.list('allocation')):
                raise FileExistsError('sandbox name is already in use')
            record = self.state.put('allocation', dict(id=identity, state='pending', desired='running',
                generation=1, released=False, request=request, spec=copy.deepcopy(spec), stamp=stamp, canonical_stamp=canonical,
                owner=owner, parent=None, ack=False, deadline=time.time() + spec['startup_timeout']),
                event={'message': 'sandbox requested'})
        return self._public_allocation(record)

    def _resolve(self, identity):
        record = self.state.get('allocation', identity, required=False)
        if record is None:
            matches = [a for a in self.state.list('allocation') if a['spec'].get('name') == identity
                       and not a.get('released')]
            if len(matches) != 1:
                raise FileNotFoundError('sandbox ID or unique name is missing: ' + identity)
            record = matches[0]
        return record

    @staticmethod
    def _public_allocation(record):
        return {k: record.get(k) for k in ('id', 'state', 'parent', 'worker', 'created', 'updated',
                                         'reason', 'error', 'released', 'info', 'deadline')}

    def allocation_get(self, identity):
        return self._public_allocation(self._resolve(identity))

    def allocation_route(self, identity):
        record = self._resolve(identity)
        if not record.get('endpoint') or not record.get('token'):
            raise ResourceUnavailable('sandbox has no acknowledged worker route: ' + record['state'])
        return dict(id=record['id'], endpoint={**record['endpoint'], 'token': record['token']},
                    owner=record.get('owner'), info=record.get('info'))

    def allocation_ack(self, identity):
        with self.state.transaction():
            record = self._resolve(identity)
            if record['state'] not in ('ready', 'leased', 'paused') or record['desired'] != 'running':
                raise ResourceUnavailable('sandbox is no longer available: ' + record['state'])
            self.state.put('allocation', {**record, 'ack': True})
        return self.allocation_route(identity)

    def allocation_cancel(self, identity):
        with self.state.transaction():
            record = self._resolve(identity)
            if record['desired'] != 'terminated':
                record.update(desired='terminated', generation=record['generation'] + 1)
                if not record.get('worker'):
                    record.update(state='terminated', released=True)
                self.state.put('allocation', record, event={'message': 'termination requested'})
        return self._public_allocation(record)

    def _launch(self, identity):
        record = self.state.get('allocation', identity)
        if (record['desired'] != 'running' or record.get('lease') or
                record['state'] not in ('reserved', 'starting', 'unknown')):
            return
        connection = None
        try:
            worker = self.state.get('worker', record['worker'])
            route = record.get('endpoint')
            if route is None:
                preparer = self.connection(worker['endpoint'])
                try:
                    prepared = preparer.call('managed_prepare', template=record['spec']['template'])
                finally:
                    preparer.close()
                route = {**worker['endpoint'], **{k: prepared['information'][k]
                         for k in ('hostname', 'port', 'workspace')}, 'token': prepared['token']}
                connection = self.connection(route)
                route = self._accept_worker(worker['id'], connection, promote=True)
            else:
                connection = self.connection(route)
            with self.state.transaction():
                record = self.state.get('allocation', identity)
                if record['desired'] != 'running' or record.get('lease'):
                    return
                record = self.state.put('allocation', {**record, 'endpoint': route, 'state': 'starting'})
            request = record['request']
            if request.get('reference'):
                from .artifacts import ensure
                pool = self.state.get('pool', record['parent'], required=False) if record.get('parent') else None
                ensure(self, request['reference'], connection, route, (pool or {}).get('shared_cache'))
            initial = connection
            connection = self.connection(route, timeout=request['spec']['startup_timeout'] + 60)
            initial.close()
            response = connection.call('managed_apply', identity=identity, cluster=self.id,
                generation=record['generation'], action='create', **request,
                process=self._owner_process(record.get('owner')))
            with self.state.transaction():
                current = self.state.get('allocation', identity)
                if current['generation'] != record['generation'] or current.get('released'):
                    return
                current.update(token=response['token'], info=response['sandbox'])
                if current['desired'] == 'running' and current['generation'] == record['generation']:
                    current['state'] = response['sandbox']['state']
                    if current['state'] == 'ready':
                        current['prepared'] = True
                        current.pop('error', None)
                    if current['state'] in scheduler.TERMINAL:
                        current['released'] = response['sandbox'].get('runtime_status', {}).get('status') not in ('running', 'paused', 'starting')
                self.state.put('allocation', current, event={'message': 'worker acknowledged creation', 'state': current['state']})
        except OperationUnknown as error:
            self._uncertain(identity, error, generation=record['generation'])
        except Exception as error:
            with self.state.transaction():
                current = self.state.get('allocation', identity)
                if current['generation'] != record['generation'] or current['desired'] != 'running':
                    return
                current.update(error=str(error), state='failed', desired='terminated', generation=current['generation'] + 1)
                self.state.put('allocation', current, event={'message': 'creation failed', 'error': str(error)})
        finally:
            if connection:
                connection.close()

    def _uncertain(self, identity, error, *, generation):
        with self.state.transaction():
            record = self.state.get('allocation', identity)
            if record['generation'] != generation or record.get('released'):
                return
            self.state.put('allocation', {**record, 'state': 'unknown', 'error': record.get('error') or str(error)})

    def _observe(self, identity):
        record = self.state.get('allocation', identity)
        if not record.get('endpoint') or record['state'] == 'claiming' or record.get('released'):
            return
        if not record.get('token') and record['desired'] == 'running':
            return self._launch(identity)
        connection = None
        try:
            connection = self.connection(record['endpoint'], timeout=5)
            info = connection.call('describe', identity=identity)
            with self.state.transaction():
                current = self.state.get('allocation', identity)
                if current['generation'] != record['generation'] or current['state'] == 'claiming' or current.get('released'):
                    return
                released = info['runtime_status']['status'] not in ('running', 'paused', 'starting') and info['state'] not in ('creating', 'preparing')
                state = info['state']
                if current.get('lease') and state == 'ready':
                    state = 'leased'
                changed = current['state'] != state
                current.update(info=info, state=state, released=released)
                if state in ('ready', 'leased', 'paused') and current['desired'] == 'running':
                    current['prepared'] = True
                    current.pop('error', None)
                self.state.put('allocation', current,
                               event={'message': 'sandbox state changed', 'state': state} if changed else None)
        except Exception as error:
            self._uncertain(identity, error, generation=record['generation'])
        finally:
            if connection:
                connection.close()

    def _terminate(self, identity):
        record = self.state.get('allocation', identity)
        if record['desired'] != 'terminated' or record.get('released'):
            return
        route = record.get('endpoint') or self.state.get('worker', record['worker'])['endpoint']
        connection = None
        try:
            connection = self.connection(route)
            response = connection.call('managed_apply', identity=identity, cluster=self.id,
                generation=record['generation'], action='terminate')
            with self.state.transaction():
                current = self.state.get('allocation', identity)
                if current['generation'] != record['generation'] or current.get('released'):
                    return
                current.update(state='terminated', released=True, info=response['sandbox'], token=response['token'], endpoint=route)
                self.state.put('allocation', current, event={'message': 'termination confirmed'})
        except Exception as error:
            self._uncertain(identity, error, generation=record['generation'])
        finally:
            if connection:
                connection.close()

    def tick(self):
        now = time.time()
        with self.guard:
            for key in list(self.pending):
                if self.pending[key].done():
                    future = self.pending.pop(key)
                    try:
                        future.result()
                    except Exception:
                        LOG.exception('Asynchronous operation failed: %s', key)
        probe = now >= self.next_probe
        if probe:
            self.next_probe = now + 2
        self._reconcile_pools()
        self._reconcile_jobs()
        observations = []
        for record in self.state.list('allocation'):
            if record.get('released'):
                continue
            if self.owners.reason(record.get('owner')) or (
                    not record.get('parent') and not record['ack'] and now >= record['deadline']):
                self.allocation_cancel(record['id'])
                record = self.state.get('allocation', record['id'])
            if record['desired'] == 'terminated' and record.get('worker'):
                self._submit(('allocation', record['id']), self._terminate, record['id'])
            elif record['state'] == 'reserved':
                self._submit(('allocation', record['id']), self._launch, record['id'])
            elif probe and record.get('endpoint') and record['state'] not in ('starting', 'pending'):
                observations.append(record['id'])
            elif record['state'] == 'starting':
                # On restart there is no in-process launch future. Resubmit the
                # identical operation to recover its result, never a new ID.
                self._submit(('allocation', record['id']), self._launch, record['id'])
        if probe:
            # Polling already-ready members must not repeatedly fill the queue
            # before a later pending launch or termination can be submitted.
            for worker in self.state.list('worker'):
                if worker['state'] != 'removed':
                    self._submit(('worker', worker['id']), self._probe, worker['id'])
            for identity in observations:
                self._submit(('allocation', identity), self._observe, identity)
        with self.state.transaction():
            allocations = self.state.list('allocation')
            pending = [a for a in allocations if a['state'] == 'pending' and a['desired'] == 'running']
            policies = {p['id']: p for p in self.state.list('pool')}
            placements, reasons = scheduler.plan(pending, self.state.list('worker'), allocations, policies, now=now)
            for identity, worker_id, gpu_uuid in placements:
                record = self.state.get('allocation', identity)
                if gpu_uuid:
                    record['spec']['_gpu_uuid'] = gpu_uuid
                    record['request']['spec']['_gpu_uuid'] = gpu_uuid
                record.update(worker=worker_id, state='reserved', reason=None)
                self.state.put('allocation', record, event={'message': 'resources reserved', 'worker': worker_id})
            for identity, reason in reasons.items():
                record = self.state.get('allocation', identity)
                if record.get('reason') != reason:
                    self.state.put('allocation', {**record, 'reason': reason}, event={'message': reason})

    def _reconcile_pools(self):
        from .pool import reconcile
        reconcile(self)

    def _reconcile_jobs(self):
        from .jobs import reconcile
        reconcile(self)

    def status(self):
        return {'id': self.id, 'protocol': PROTOCOL, 'workers': self.worker_list(),
                'sandboxes': [self._public_allocation(a) for a in self.state.list('allocation')],
                'pools': [self.pool_status(p['id']) for p in self.state.list('pool')],
                'jobs': [self.job_status(j['id']) for j in self.state.list('job')]}

    def dispatch(self, operation, parameters):
        if operation == 'dashboard_ticket':
            return self.dashboard.ticket()
        if operation == 'relay_poll':
            return self.relay.poll(**parameters)
        if operation == 'relay_result':
            return self.relay.result(**parameters)
        if operation == 'ping':
            return {'cluster_id': self.id, 'protocol': PROTOCOL,
                    'pool_options': ['shared_cache', 'affinity']}
        if operation == 'events':
            return self.state.events(**parameters)
        if operation == 'backup':
            return self.state.backup(**parameters)
        if operation.startswith('pool_'):
            from .pool import dispatch
            return dispatch(self, operation, parameters)
        if operation.startswith('job_'):
            from .jobs import dispatch
            return dispatch(self, operation, parameters)
        if operation.startswith('snapshot_') or operation == 'snapshot_register':
            from .artifacts import dispatch
            return dispatch(self, operation, parameters)
        if operation not in {'create', 'allocation_get', 'allocation_route', 'allocation_ack',
                             'allocation_cancel', 'worker_add', 'worker_list', 'worker_update',
                             'owner_register', 'owner_heartbeat', 'owner_routes', 'status', 'sandbox_rpc'}:
            raise ValueError('unknown cluster operation: ' + operation)
        return getattr(self, operation)(**parameters)

    def pool_status(self, identity):
        from .pool import status
        return status(self, identity)

    def job_status(self, identity):
        from .jobs import status
        return status(self, identity)

    def close(self):
        # Stopping this process leaves durable desired state and live guests.
        self.stopping.set()
        self.relay.close()
        if self.thread:
            self.thread.join()
        self.executor.shutdown(wait=True)
        self.state.close()

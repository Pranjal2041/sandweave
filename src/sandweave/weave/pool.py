"""The public pool contract, with durable coordination for cluster targets."""
from contextlib import contextmanager
import copy
import threading
import time
import uuid

from ..sandbox.pool import Pool as LocalPool, Lease
from ..sandbox.asyncio import dualmethod, dualclassmethod
from ..sandbox.errors import ResourceUnavailable, OperationUnknown
from ..sandbox.ownership import client_owner
from ..sandbox.resources import positive
from . import scheduler


class Pool(LocalPool):
    def __new__(cls, *args, **kwargs):
        from .client import cluster_config
        if cls is Pool and cluster_config(kwargs.get('target')) is not None:
            return ManagedPool(*args, **kwargs)
        return super().__new__(cls)

    @dualclassmethod
    def connect(cls, identity, *, target):
        return ManagedPool.connect(identity, target=target)


class ManagedPool(LocalPool):
    def __init__(self, *, target, size=1, warm=0, weight=1, priority=0, labels=None,
                 placement='spread', wait_timeout=300, **options):
        from .client import ClusterConnection, cluster_config
        validate(size=size, warm=warm, weight=weight, priority=priority, labels=labels or {}, placement=placement)
        if wait_timeout is not None:
            positive(wait_timeout, 'wait_timeout')
        self.size, self.warm, self.target = size, warm, target
        self.policy = dict(size=size, warm=warm, weight=weight, priority=priority, labels=labels or {}, placement=placement)
        self.id = 'pool-' + uuid.uuid4().hex
        self.name = options.pop('name', None)
        self.options = options
        self.wait_timeout = wait_timeout
        self.connection = ClusterConnection(cluster_config(target))
        self.started = self.closed = self.initial_ready = False
        self.owned = True
        self.lock = threading.RLock()

    @dualclassmethod
    def connect(cls, identity, *, target):
        self = cls(target=target)
        self.id = self.connection.call('pool_status', identity=str(identity))['id']
        info = self.connection.call('pool_status', identity=self.id)
        self.size, self.warm = info['size'], info['warm']
        self.started, self.initial_ready, self.owned = True, True, False
        return self

    @property
    def info(self):
        return self.connection.call('pool_status', identity=self.id)

    def _declare(self):
        from ..sandbox.sandbox import definition
        with self.lock:
            if self.closed:
                raise RuntimeError('pool is closed')
            if not self.started:
                request = definition(target=self.target, **self.options)
                owner = None if self.options.get('detached') else client_owner(self.connection)
                self.connection.call('pool_create', identity=self.id, name=self.name, request=request,
                                     owner=owner, **self.policy)
                self.started = True

    @dualmethod
    def start(self):
        self._declare()
        if self.initial_ready:
            return self
        deadline = None if self.wait_timeout is None else time.monotonic() + self.wait_timeout
        try:
            while True:
                info = self.info
                if info['state'] in ('failed', 'closed'):
                    raise ResourceUnavailable(info.get('error') or 'pool is ' + info['state'])
                if info['state'] == 'ready' and info['ready'] >= min(self.warm, self.size - info['active']):
                    self.initial_ready = True
                    return self
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError('pool is waiting: ' + (info.get('reason') or info['state']))
                time.sleep(.05)
        except BaseException:
            if self.owned:
                self.terminate()
            raise

    @start.async_impl
    async def _start_async(self):
        # Reuse the established cancellation/drain behavior of local pools.
        return await LocalPool._start_async(self)

    def acquire(self, *, timeout=None):
        cancelled = threading.Event()
        return Lease(self._lease(cancelled, self.wait_timeout if timeout is None else timeout), cancelled)

    @contextmanager
    def _lease(self, cancelled, timeout):
        from ..sandbox.sandbox import Sandbox
        self.start()
        identity = uuid.uuid4().hex
        owner = client_owner(self.connection)
        deadline = None if timeout is None else time.monotonic() + positive(timeout, 'timeout')
        self.connection.call('pool_checkout', identity=self.id, lease_id=identity, owner=owner)
        env = None
        try:
            while True:
                if cancelled.is_set():
                    raise InterruptedError('pool checkout cancelled')
                info = self.connection.call('pool_lease', identity=self.id, lease_id=identity)
                if info['state'] == 'ready':
                    self.connection.remember(info['route'])
                    env = Sandbox.connect(info['sandbox'], target=self.target)
                    yield env
                    return
                if info['state'] in ('failed', 'cancelled'):
                    raise ResourceUnavailable(info.get('error') or 'pool checkout was cancelled')
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError('pool checkout is waiting: ' + (self.info.get('reason') or 'capacity is in use'))
                time.sleep(.05)
        finally:
            try:
                self.connection.call('pool_release', identity=self.id, lease_id=identity)
            finally:
                if env is not None:
                    self.connection.forget(env.id)
                    env.close()

    @dualmethod
    def update(self, **changes):
        result = self.connection.call('pool_update', identity=self.id, **changes)
        self.size, self.warm = result['size'], result['warm']
        return result

    @dualmethod
    def submit(self, command, **options):
        from .jobs import Job
        self.start()
        return Job.submit(command, target=self.target, pool=self.id, **options)

    @dualmethod
    def terminate(self):
        if self.started:
            self.connection.call('pool_close', identity=self.id)
            deadline = None if self.wait_timeout is None else time.monotonic() + self.wait_timeout
            while True:
                info = self.info
                if info['state'] == 'closed':
                    return info
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError('pool cleanup is still pending; its desired state remains closed')
                time.sleep(.05)

    @dualmethod
    def close(self):
        if not self.closed:
            try:
                if self.owned:
                    self.terminate()
            finally:
                self.connection.close()
                self.closed = True


def validate(*, size, warm, weight, priority, labels, placement):
    positive(size, 'size', integer=True)
    if type(warm) is not int or not 0 <= warm <= size:
        raise ValueError('warm must be in 0..size')
    positive(weight, 'weight')
    if type(priority) is not int:
        raise ValueError('priority must be an integer')
    if not isinstance(labels, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in labels.items()):
        raise ValueError('labels must map strings to strings')
    if placement not in ('spread', 'pack'):
        raise ValueError('placement must be spread or pack')


def resolve(controller, identity):
    record = controller.state.get('pool', identity, required=False)
    if record is None:
        matches = [p for p in controller.state.list('pool') if p.get('name') == identity and p['state'] != 'closed']
        if len(matches) != 1:
            raise FileNotFoundError('pool ID or unique name is missing: ' + identity)
        record = matches[0]
    return record


def status(controller, identity):
    pool = resolve(controller, identity)
    allocations = controller.state.list('allocation', parent=pool['id'])
    live = [a for a in allocations if not a.get('released')]
    return {**{k: pool.get(k) for k in ('id', 'name', 'state', 'size', 'warm', 'weight', 'priority',
                                       'labels', 'placement', 'error', 'baseline')},
            'ready': sum(a['state'] == 'ready' and not a.get('lease') and a.get('role') != 'builder' and a['desired'] == 'running' for a in live),
            'active': sum(bool(a.get('lease')) for a in live),
            'pending': sum(a['state'] in ('pending', 'reserved', 'starting', 'unknown') for a in live),
            'waiting': sum(l['state'] == 'pending' for l in controller.state.list('lease', parent=pool['id'])),
            'reason': next((a.get('reason') or a.get('error') for a in live if a.get('reason') or a.get('error')), None),
            'sandboxes': [a['id'] for a in live]}


def dispatch(controller, operation, params):
    state = controller.state
    identity = params['identity']
    if operation == 'pool_create':
        policy = {k: params[k] for k in ('size', 'warm', 'weight', 'priority', 'labels', 'placement')}
        validate(**policy)
        controller._owner_process(params.get('owner'))
        with state.transaction():
            existing = state.get('pool', identity, required=False)
            if existing is None:
                if params.get('name') and any(p.get('name') == params['name'] and p['state'] != 'closed' for p in state.list('pool')):
                    raise FileExistsError('pool name is already in use')
                state.put('pool', dict(id=identity, state='preparing', desired='running',
                    name=params.get('name'), owner=params.get('owner'), request=params['request'],
                    baseline=params['request'].get('reference'), failures=0, **policy), event={'message': 'pool requested'})
            elif existing['request'] != params['request'] or existing.get('owner') != params.get('owner'):
                raise FileExistsError('pool ID is already in use')
        return status(controller, identity)
    pool = resolve(controller, identity)
    identity = pool['id']
    if operation == 'pool_status':
        return status(controller, identity)
    if operation == 'pool_update':
        allowed = {'size', 'warm', 'weight', 'priority', 'labels', 'placement'}
        if params.keys() - allowed - {'identity'}:
            raise ValueError('pool updates support size, warm, weight, priority, labels and placement')
        with state.transaction():
            pool = resolve(controller, identity)
            pool.update({k: v for k, v in params.items() if k in allowed})
            validate(**{k: pool[k] for k in allowed})
            state.put('pool', pool, event={'message': 'pool policy updated'})
        return status(controller, identity)
    if operation == 'pool_close':
        with state.transaction():
            pool = resolve(controller, identity)
            state.put('pool', {**pool, 'desired': 'closed'}, event={'message': 'pool closure requested'})
            for allocation in state.list('allocation', parent=identity):
                controller.allocation_cancel(allocation['id'])
            for lease in state.list('lease', parent=identity):
                if lease['state'] not in ('released', 'cancelled'):
                    state.put('lease', {**lease, 'state': 'cancelled'})
        return status(controller, identity)
    lease_id = params['lease_id']
    if operation == 'pool_checkout':
        controller._owner_process(params.get('owner'))
        with state.transaction():
            pool = resolve(controller, identity)
            if pool['desired'] != 'running' or pool['state'] == 'failed':
                raise ResourceUnavailable('pool is closed or failed')
            old = state.get('lease', lease_id, required=False)
            if old is None:
                state.put('lease', dict(id=lease_id, parent=identity, state='pending', owner=params.get('owner')))
            elif old['parent'] != identity or old.get('owner') != params.get('owner'):
                raise FileExistsError('lease ID belongs to another request')
        return {'id': lease_id}
    with state.transaction():
        lease = state.get('lease', lease_id)
        if lease['parent'] != identity:
            raise PermissionError('lease belongs to another pool')
        if operation == 'pool_release':
            if lease.get('sandbox'):
                controller.allocation_cancel(lease['sandbox'])
            state.put('lease', {**lease, 'state': 'released' if lease.get('sandbox') else 'cancelled'})
            return {'id': lease_id, 'state': 'released'}
        if operation == 'pool_lease':
            result = {k: lease.get(k) for k in ('id', 'state', 'sandbox', 'error')}
            if lease['state'] == 'ready':
                result['route'] = controller.allocation_route(lease['sandbox'])
            return result
    raise ValueError('unknown pool operation: ' + operation)


def _new_member(controller, pool, *, builder=False):
    request = copy.deepcopy(pool['request'])
    if not builder:
        # The builder resolves image metadata and its private control runtime.
        # Members must inherit that prepared definition with the filesystem.
        request['spec'] = copy.deepcopy(pool.get('baseline_spec', request['spec']))
        request.update(reference=pool['baseline'], cache_key=None, refresh=False)
    request['spec'].update(detached=True, ttl=None, name=None)
    identity = ('vr-sw-' if 'vr' in request['spec']['template']['capabilities'] else 'sw-') + uuid.uuid4().hex
    with controller.state.transaction():
        pool = resolve(controller, pool['id'])
        if pool['desired'] != 'running' or pool['state'] == 'failed':
            return
        controller.create(identity, **request, owner=None, operation_id=uuid.uuid4().hex)
        record = controller.state.get('allocation', identity)
        controller.state.put('allocation', {**record, 'parent': pool['id'], 'role': 'builder' if builder else 'member', 'ack': True})
        if builder:
            controller.state.put('pool', {**pool, 'builder': identity})


def _capture(controller, pool_id, builder_id):
    from . import providers
    try:
        record = controller.state.get('allocation', builder_id)
        connection = controller.connection(record['endpoint'])
        try:
            saved = connection.call('capture', identity=builder_id, state='filesystem')
            verification = connection.call('snapshot_verify', reference=saved['id'])
            if verification.get('status') != 'passed':
                raise ResourceUnavailable('pool baseline verification failed: ' + str(verification.get('error')))
            from .artifacts import register
            prepared = register(controller, saved['id'], record['endpoint'])['spec']
        finally:
            connection.close()
        with controller.state.transaction():
            pool = resolve(controller, pool_id)
            controller.state.put('pool', {**pool, 'baseline': saved['id'],
                                         'baseline_spec': prepared, 'state': 'ready'})
            controller.allocation_cancel(builder_id)
    except Exception as error:
        with controller.state.transaction():
            pool = resolve(controller, pool_id)
            controller.state.put('pool', {**pool, 'state': 'failed', 'error': str(error)})
            controller.allocation_cancel(builder_id)


def _claim(controller, pool_id, lease_id, sandbox_id):
    from . import providers
    lease = controller.state.get('lease', lease_id)
    record = controller.state.get('allocation', sandbox_id)
    pool = resolve(controller, pool_id)
    connection = controller.connection(record['endpoint'])
    try:
        response = connection.call('managed_apply', identity=sandbox_id, cluster=controller.id,
            generation=record['generation'], action='claim', owner=lease.get('owner'),
            process=controller._owner_process(lease.get('owner')), spec=pool['request']['spec'])
        with controller.state.transaction():
            lease = controller.state.get('lease', lease_id)
            record = controller.state.get('allocation', sandbox_id)
            record.update(token=response['token'], info=response['sandbox'], owner=lease.get('owner'))
            if lease['state'] == 'claiming' and record['desired'] == 'running':
                record['state'] = 'leased'
                controller.state.put('lease', {**lease, 'state': 'ready'})
            controller.state.put('allocation', record)
    except OperationUnknown:
        # Recover the same committed lease and assignment generation on retry.
        return
    except Exception as error:
        with controller.state.transaction():
            lease = controller.state.get('lease', lease_id)
            if lease['state'] == 'claiming':
                controller.state.put('lease', {**lease, 'state': 'failed', 'error': str(error)})
            controller.allocation_cancel(sandbox_id)
    finally:
        connection.close()


def reconcile(controller):
    state = controller.state
    for pool in state.list('pool'):
        if pool['state'] == 'closed':
            continue
        if controller.owners.reason(pool.get('owner')):
            dispatch(controller, 'pool_close', {'identity': pool['id']})
            pool = resolve(controller, pool['id'])
        allocations = state.list('allocation', parent=pool['id'])
        live = [a for a in allocations if not a.get('released')]
        if pool['desired'] == 'closed':
            for allocation in live:
                controller.allocation_cancel(allocation['id'])
            if not live:
                state.put('pool', {**pool, 'state': 'closed'})
            continue
        if pool['state'] == 'failed':
            continue
        failed = [a for a in allocations if a.get('error') and a.get('released') and a.get('role') == 'member']
        latest_ready = max((a['created'] for a in allocations if a.get('info', {}).get('state') == 'ready'), default=0)
        if sum(a['created'] > latest_ready for a in failed) >= 3:
            state.put('pool', {**pool, 'state': 'failed', 'error': 'pool preparation failed three times: ' + failed[-1]['error']})
            continue
        if pool.get('baseline') is None:
            if not pool.get('builder'):
                _new_member(controller, pool, builder=True)
            else:
                builder = state.get('allocation', pool['builder'])
                if builder['state'] == 'ready':
                    controller._submit(('capture', pool['id']), _capture, controller, pool['id'], builder['id'])
                elif builder['state'] in ('failed', 'terminated'):
                    state.put('pool', {**pool, 'state': 'failed', 'error': builder.get('error') or 'baseline preparation failed'})
            continue
        if pool['state'] != 'ready':
            pool = state.put('pool', {**pool, 'state': 'ready'})
        for lease in state.list('lease', parent=pool['id']):
            if lease['state'] not in ('ready', 'claiming', 'pending'):
                continue
            if controller.owners.reason(lease.get('owner')):
                dispatch(controller, 'pool_release', {'identity': pool['id'], 'lease_id': lease['id']})
            elif lease['state'] == 'claiming':
                controller._submit(('claim', lease['id']), _claim, controller, pool['id'], lease['id'], lease['sandbox'])
        with state.transaction():
            pool = resolve(controller, pool['id'])
            if pool['desired'] != 'running':
                continue
            live = [a for a in state.list('allocation', parent=pool['id']) if not a.get('released')]
            ready = [a for a in live if a['state'] == 'ready' and not a.get('lease') and a.get('role') != 'builder' and a['desired'] == 'running']
            waiting = state.list('lease', state='pending', parent=pool['id'])
            for lease, allocation in zip(waiting, ready):
                state.put('lease', {**lease, 'sandbox': allocation['id'], 'state': 'claiming'})
                state.put('allocation', {**allocation, 'lease': lease['id'], 'state': 'claiming',
                                        'generation': allocation['generation'] + 1})
            waiting_count = max(0, len(waiting) - len(ready))
            ready_count = max(0, len(ready) - len(waiting))
            preparing = sum(a['state'] in ('pending', 'reserved', 'starting', 'unknown') and not a.get('lease') for a in live)
            desired = min(pool['size'] - len(live), max(pool['warm'] - ready_count, waiting_count) - preparing)
            for _ in range(max(0, desired)):
                _new_member(controller, pool)
            # Shrinking and draining affect idle members; active episodes finish.
            excess = max(0, len(live) - pool['size'])
            for allocation in ready[len(waiting):]:
                worker = state.get('worker', allocation['worker'])
                if excess > 0 or worker.get('draining'):
                    controller.allocation_cancel(allocation['id'])
                    excess = max(0, excess - 1)

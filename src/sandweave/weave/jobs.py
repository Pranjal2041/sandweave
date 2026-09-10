"""Durable commands with explicit attempts, result commits and retry policies."""
import json
from pathlib import Path
import subprocess
import time
import uuid

from ..sandbox.asyncio import dualmethod, dualclassmethod
from ..sandbox.errors import ResourceUnavailable, OperationUnknown, CommandError, CommandTimeout, OutputLimitExceeded
from ..sandbox.ownership import client_owner
from ..sandbox.process import CommandResult
from ..sandbox.resources import positive
from . import providers
from .pool import dispatch as pool_dispatch

TERMINAL = {'succeeded', 'failed', 'cancelled'}


class Job:
    def __init__(self, identity, connection):
        self.id, self.connection = identity, connection

    @dualclassmethod
    def submit(cls, command, *, target='lab', pool=None, files=None, items=None, detached=False,
               retries=0, retry_codes=(), retry_infrastructure=False, timeout=None,
               max_output_bytes=4*1024**2, every=None, **sandbox_options):
        from .client import ClusterConnection, cluster_config
        from .pool import ManagedPool
        validate(command, retries, retry_codes, retry_infrastructure, timeout, max_output_bytes, every)
        if type(detached) is not bool:
            raise ValueError('detached must be a bool')
        bundled = {}
        for destination, source in (files or {}).items():
            path = Path(destination)
            if path == Path('.') or path.is_absolute() or '..' in path.parts:
                raise ValueError('job file destinations must be relative to /workspace')
            bundled[str(path)] = source if isinstance(source, bytes) else Path(source).read_bytes()
        values = [None] if items is None else list(items)
        if not values:
            raise ValueError('job items must not be empty')
        json.dumps(values, allow_nan=False)
        connection = ClusterConnection(cluster_config(target))
        owner = None if detached else client_owner(connection)
        owned_pool = None
        pool_request = None
        if pool is None:
            from ..sandbox.sandbox import definition
            owned_pool = ManagedPool(target=target, detached=detached, **sandbox_options)
            pool_request = dict(identity=owned_pool.id, name=owned_pool.name,
                request=definition(target=target, **owned_pool.options), owner=owner, **owned_pool.policy)
            pool = owned_pool.id
        elif sandbox_options:
            raise ValueError('sandbox configuration belongs to the existing pool')
        identity = 'job-' + uuid.uuid4().hex
        request = dict(command=command, files=bundled, items=values, retries=retries,
                       retry_codes=list(retry_codes), retry_infrastructure=retry_infrastructure,
                       timeout=timeout, max_output_bytes=max_output_bytes, every=every)
        try:
            connection.call('job_create', identity=identity, pool=getattr(pool, 'id', str(pool)), owner=owner,
                            owned_pool=owned_pool is not None, pool_request=pool_request, request=request)
        except BaseException:
            connection.close()
            raise
        finally:
            if owned_pool is not None:
                # The job now owns pool lifetime; disconnecting this client must
                # not invoke the owned Pool context's termination behavior.
                owned_pool.connection.close()
        return cls(identity, connection)

    @dualclassmethod
    def connect(cls, identity, *, target='lab'):
        from .client import ClusterConnection, cluster_config
        self = cls(str(identity), ClusterConnection(cluster_config(target)))
        self.info
        return self

    @property
    def info(self):
        info = self.connection.call('job_status', identity=self.id)
        self.connection.acknowledge_routes(info.pop('_owner_routes', []))
        return info

    @dualmethod
    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + positive(timeout, 'timeout')
        while True:
            info = self.info
            if info['state'] in TERMINAL:
                return info
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('job is still running; waiting did not cancel it')
            time.sleep(.05)

    @dualmethod
    def result(self, *, timeout=None, check=False):
        info = self.wait(timeout)
        if info['state'] == 'cancelled':
            raise ResourceUnavailable('job was cancelled')
        result = self.connection.call('job_results', identity=self.id)
        values = []
        for record in result:
            if record.get('failure') == 'infrastructure':
                raise ResourceUnavailable(record['error'])
            value = CommandResult(stdout=record['stdout'].decode(errors='replace'),
                                  stderr=record['stderr'].decode(errors='replace'), returncode=record['returncode'],
                                  output_limited=record.get('output_limited', False))
            if record.get('timed_out'):
                raise CommandTimeout('job command timed out', result=value)
            if value.output_limited:
                raise OutputLimitExceeded('job command exceeded its output limit', result=value)
            if check and value.returncode:
                raise CommandError('job command exited with ' + str(value.returncode), result=value)
            values.append(value)
        return values[0] if len(values) == 1 else values

    @dualmethod
    def cancel(self):
        return self.connection.call('job_cancel', identity=self.id)

    def close(self):
        self.connection.close()


def validate(command, retries, retry_codes, retry_infrastructure, timeout, max_output_bytes, every):
    if not isinstance(command, str) or not command or '\0' in command:
        raise ValueError('job command must be a nonempty command string')
    if type(retries) is not int or retries < 0:
        raise ValueError('retries must be a nonnegative integer')
    if any(type(c) is not int for c in retry_codes):
        raise ValueError('retry_codes must contain integer exit codes')
    if type(retry_infrastructure) is not bool:
        raise ValueError('retry_infrastructure must be a bool')
    if timeout is not None:
        positive(timeout, 'timeout')
    positive(max_output_bytes, 'max_output_bytes', integer=True)
    if max_output_bytes > 16*1024**2:
        raise ValueError('durable job output is limited to 16 MiB per attempt')
    if every is not None:
        positive(every, 'every')


def status(controller, identity):
    job = controller.state.get('job', identity)
    tasks = controller.state.list('task', parent=identity)
    return {**{k: job.get(k) for k in ('id', 'state', 'pool', 'created', 'updated', 'error')},
            'tasks': [{k: t.get(k) for k in ('id', 'index', 'state', 'attempt', 'sandbox', 'error')} for t in tasks],
            'succeeded': sum(t['state'] == 'succeeded' for t in tasks),
            'failed': sum(t['state'] == 'failed' for t in tasks)}


def dispatch(controller, operation, params):
    state, identity = controller.state, params['identity']
    if operation == 'job_create':
        request = params['request']
        validate(**{k: request[k] for k in ('command', 'retries', 'retry_codes', 'retry_infrastructure', 'timeout', 'max_output_bytes', 'every')})
        if not isinstance(request['items'], list) or not request['items']:
            raise ValueError('job items must be a nonempty list')
        json.dumps(request['items'], allow_nan=False)
        for name, value in request['files'].items():
            path = Path(name)
            if path == Path('.') or path.is_absolute() or '..' in path.parts or not isinstance(value, bytes):
                raise ValueError('job files must map relative file destinations to bytes')
        controller._owner_process(params.get('owner'))
        with state.transaction():
            old = state.get('job', identity, required=False)
            if old is None:
                if params.get('pool_request') is not None:
                    if params['pool_request']['identity'] != params['pool']:
                        raise ValueError('job and pool identities disagree')
                    # One transaction publishes both. A detached submitter that
                    # crashes cannot leave an anonymous pool without its job.
                    pool_dispatch(controller, 'pool_create', params['pool_request'])
                pool = state.get('pool', params['pool'])
                if pool['desired'] != 'running' or pool['state'] == 'failed':
                    raise ResourceUnavailable('job pool is closed')
                state.put('job', dict(id=identity, state='running', pool=pool['id'],
                    owner=params.get('owner'), owned_pool=params.get('owned_pool', False), request=request),
                    event={'message': 'job submitted'})
                for index, item in enumerate(request['items']):
                    state.put('task', dict(id=identity + '-' + str(index), parent=identity,
                        index=index, item=item, state='pending', attempt=0, due=time.time()))
            elif old['request'] != request or old.get('owner') != params.get('owner') or old['pool'] != params['pool']:
                raise FileExistsError('job ID is already in use')
        return status(controller, identity)
    if operation == 'job_status':
        result = status(controller, identity)
        job = state.get('job', identity)
        routes = []
        if job.get('owner'):
            for task in state.list('task', parent=identity):
                lease = state.get('lease', task.get('lease', ''), required=False)
                if lease and lease['state'] == 'ready':
                    routes.append(controller.allocation_route(lease['sandbox']))
        return {**result, '_owner_routes': routes}
    if operation == 'job_cancel':
        with state.transaction():
            job = state.get('job', identity)
            if job['state'] not in TERMINAL:
                state.put('job', {**job, 'state': 'cancelled'}, event={'message': 'job cancelled'})
                for task in state.list('task', parent=identity):
                    if task.get('lease'):
                        pool_dispatch(controller, 'pool_release', {'identity': job['pool'], 'lease_id': task['lease']})
                    if task['state'] not in TERMINAL:
                        state.put('task', {**task, 'state': 'cancelled'})
                if job['owned_pool']:
                    pool_dispatch(controller, 'pool_close', {'identity': job['pool']})
        return status(controller, identity)
    if operation == 'job_results':
        tasks = sorted(state.list('task', parent=identity), key=lambda t: t['index'])
        if any(t['state'] not in TERMINAL for t in tasks):
            raise ResourceUnavailable('job has unfinished tasks')
        return [t.get('result', {'failure': 'infrastructure', 'error': t.get('error') or 'task was cancelled'}) for t in tasks]
    raise ValueError('unknown job operation')


def _step(controller, task_id):
    state = controller.state
    task = state.get('task', task_id)
    job = state.get('job', task['parent'])
    if job['state'] != 'running' or task['state'] in TERMINAL:
        return
    connection = None
    try:
        lease = pool_dispatch(controller, 'pool_lease', {'identity': job['pool'], 'lease_id': task['lease']})
        if lease['state'] == 'pending' or lease['state'] == 'claiming':
            return
        if lease['state'] != 'ready':
            raise ResourceUnavailable(lease.get('error') or 'job lease was lost')
        route = lease['route']
        if job.get('owner'):
            allocation = state.get('allocation', route['id'])
            process = controller._owner_process(job['owner'])
            worker = state.get('worker', allocation['worker'])
            if (allocation.get('owner_route_ack') != job['owner'] and
                    (not process or process['scope'] != worker['inventory']['scope'])):
                # An attached remote command starts only after its creator can
                # renew the worker directly, even if the controller disappears.
                return
        try:
            connection = controller.connection(route['endpoint'], timeout=10)
        except (OSError, subprocess.SubprocessError, ResourceUnavailable) as error:
            raise OperationUnknown('job worker cannot be reached') from error
        request = job['request']
        if task['state'] == 'waiting':
            for name, data in request['files'].items():
                relative = Path(name)
                if relative.is_absolute() or '..' in relative.parts:
                    raise ValueError('job file escapes /workspace')
                for offset in range(0, max(1, len(data)), 1024**2):
                    connection.call('file', identity=route['id'], op='write', path='/workspace/' + name,
                                    data=data[offset:offset+1024**2], offset=offset, truncate=offset == 0)
            with state.transaction():
                task = state.get('task', task_id)
                if task['state'] != 'waiting':
                    return
                task = state.put('task', {**task, 'state': 'starting', 'sandbox': route['id']})
        if task['state'] == 'starting':
            connection.call('command_start', identity=route['id'], process_id=task['process'], command=request['command'],
                env={'SANDWEAVE_ITEM': json.dumps(task['item']), 'SANDWEAVE_TASK_ID': task['id'],
                     'SANDWEAVE_ATTEMPT': str(task['attempt'])},
                timeout=request['timeout'], max_output_bytes=request['max_output_bytes'])
            connection.call('process_stdin', identity=route['id'], process_id=task['process'], close=True)
            with state.transaction():
                task = state.get('task', task_id)
                if task['state'] != 'starting':
                    return
                task = state.put('task', {**task, 'state': 'running'})
        outcome = connection.call('process_status', identity=route['id'], process_id=task['process'])
        if outcome['returncode'] is None:
            return
        result = {'returncode': outcome['returncode'], 'timed_out': outcome.get('timed_out', False),
                  'output_limited': outcome.get('output_limited', False),
                  'failure': 'infrastructure' if outcome.get('cold_boot') else 'application' if outcome['returncode'] else None}
        for stream in ('stdout', 'stderr'):
            pieces, offset = [], 0
            while offset < outcome[stream + '_size']:
                data = connection.call('process_output', identity=route['id'], process_id=task['process'],
                                       stream=stream, offset=offset, size=1024**2)
                if not data:
                    raise OSError('job output ended before its recorded size')
                pieces.append(data); offset += len(data)
            result[stream] = b''.join(pieces)
        _finish(controller, task_id, result)
    except OperationUnknown:
        # The same task/process ID is retained. An unavailable worker is not
        # evidence that its command did not execute.
        return
    except Exception as error:
        task = state.get('task', task_id)
        allocation = state.get('allocation', task.get('sandbox', ''), required=False)
        if (isinstance(error, ResourceUnavailable) and allocation and
                not allocation.get('released')):
            # Preserve uncertain active work. Surface the diagnostic for users.
            with state.transaction():
                task = state.get('task', task_id)
                if task['state'] not in TERMINAL:
                    state.put('task', {**task, 'error': str(error)})
            return
        _finish(controller, task_id, {'failure': 'infrastructure', 'error': str(error),
                                    'returncode': -1, 'stdout': b'', 'stderr': b''})
    finally:
        if connection:
            connection.close()


def _finish(controller, task_id, result):
    state = controller.state
    with state.transaction():
        task = state.get('task', task_id)
        if task['state'] in TERMINAL:
            return
        job = state.get('job', task['parent'])
        request = job['request']
        state.put('attempt', dict(id=task_id + '-' + str(task['attempt']), parent=task_id, result=result))
        retry = task['attempt'] < request['retries'] and (
            result.get('failure') == 'infrastructure' and request['retry_infrastructure'] or
            result['returncode'] in request['retry_codes'])
        repeating = request['every'] is not None and job['state'] == 'running'
        if retry or repeating:
            task.update(state='pending', attempt=task['attempt'] + 1,
                        due=time.time() + (request['every'] if repeating else min(30, 2**task['attempt'])))
        else:
            task.update(state='succeeded' if result['returncode'] == 0 else 'failed', result=result)
        state.put('task', task, event={'message': 'attempt completed', 'returncode': result['returncode'], 'failure': result.get('failure')})
        pool_dispatch(controller, 'pool_release', {'identity': job['pool'], 'lease_id': task['lease']})


def reconcile(controller):
    state = controller.state
    for job in state.list('job', state='running'):
        if controller.owners.reason(job.get('owner')):
            dispatch(controller, 'job_cancel', {'identity': job['id']})
            continue
        tasks = state.list('task', parent=job['id'])
        pool = state.get('pool', job['pool'])
        if pool['state'] == 'failed' or pool['desired'] != 'running':
            with state.transaction():
                for task in state.list('task', parent=job['id']):
                    if task['state'] in TERMINAL:
                        continue
                    if task.get('lease'):
                        pool_dispatch(controller, 'pool_release', {'identity': job['pool'], 'lease_id': task['lease']})
                    error = pool.get('error') or 'job pool was closed'
                    state.put('task', {**task, 'state': 'failed', 'error': error,
                        'result': {'failure': 'infrastructure', 'error': error, 'returncode': -1, 'stdout': b'', 'stderr': b''}})
                current = state.get('job', job['id'])
                if current['state'] == 'running':
                    state.put('job', {**current, 'state': 'failed', 'error': pool.get('error') or 'job pool was closed'})
                if job['owned_pool']:
                    pool_dispatch(controller, 'pool_close', {'identity': job['pool']})
            continue
        if all(t['state'] in TERMINAL for t in tasks):
            with state.transaction():
                current = state.get('job', job['id'])
                if current['state'] == 'running':
                    state.put('job', {**current, 'state': 'succeeded' if all(t['state'] == 'succeeded' for t in tasks) else 'failed'})
                    if job['owned_pool']:
                        pool_dispatch(controller, 'pool_close', {'identity': job['pool']})
            continue
        for task in tasks:
            if task['state'] == 'pending' and time.time() >= task['due']:
                previous = state.get('allocation', task.get('sandbox', ''), required=False)
                if previous and not previous.get('released'):
                    continue  # Replacements wait for confirmed stop, not lease expiry.
                with state.transaction():
                    task = state.get('task', task['id'])
                    if task['state'] != 'pending' or state.get('job', job['id'])['state'] != 'running':
                        continue
                    lease_id = uuid.uuid4().hex
                    pool_dispatch(controller, 'pool_checkout', {'identity': job['pool'], 'lease_id': lease_id, 'owner': job.get('owner')})
                    state.put('task', {**task, 'state': 'waiting', 'lease': lease_id, 'process': uuid.uuid4().hex})
            elif task['state'] in ('waiting', 'starting', 'running'):
                controller._submit(('task', task['id']), _step, controller, task['id'])

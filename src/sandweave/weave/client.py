"""Named cluster targets and direct connections to assigned sandboxes."""
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time

from . import providers
from ..sandbox.asyncio import dualmethod, dualclassmethod
from ..sandbox.errors import OperationUnknown, ResourceUnavailable, SandboxError
from ..sandbox.workspace import home, atomic_json, locked
from ..sandbox.targets import _ssh

_registries = {}
_registry_lock = threading.Lock()


def cluster_config(target):
    if isinstance(target, Cluster):
        return target.config
    if isinstance(target, dict):
        return target.get('cluster')
    if isinstance(target, str) and target not in ('local', '') and not target.startswith('ssh://'):
        path = home() / 'config.json'
        if path.is_file():
            return json.loads(path.read_text()).get('targets', {}).get(target, {}).get('cluster')
    return None


def metadata(config):
    path = str(Path(config['directory']) / 'controller.json')
    if config['hostname'] == socket.gethostname():
        return json.loads(Path(path).read_text())
    return json.loads(_ssh(config.get('ssh_host', config['hostname']),
        [config.get('python', 'python3'), '-c', 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())', path]))


def save_target(name, config):
    if not isinstance(name, str) or name == 'local' or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', name):
        raise ValueError('cluster name must use letters, digits, underscores or hyphens; local is reserved')
    path = home() / 'config.json'
    with locked(path.with_suffix('.lock')):
        saved = json.loads(path.read_text()) if path.exists() else {}
        old = saved.setdefault('targets', {}).get(name)
        if old is not None and old != {'cluster': config}:
            raise FileExistsError('target name already identifies another target: ' + name)
        saved['targets'][name] = {'cluster': config}
        atomic_json(path, saved)


class ClusterConnection:
    def __init__(self, config, *, timeout=300):
        if config is None:
            raise ValueError('target is not a configured cluster')
        self.config, self.timeout = dict(config), timeout
        info = metadata(self.config)
        self.endpoint = {k: info[k] for k in ('hostname', 'port', 'token')}
        if config.get('ssh_host'):
            self.endpoint['ssh_host'] = config['ssh_host']
        self.control = providers.direct(self.endpoint, timeout=timeout)
        self.host, self.port, self.token = info['hostname'], info['port'], info['token']
        self.unix_path = None
        key = (os.getpid(), self.host, str(config['directory']), self.token)
        with _registry_lock:
            self.registry = _registries.setdefault(key, {'routes': {}, 'owners': {}, 'lock': threading.RLock()})
        self.connections, self.heartbeats = {}, {}
        self.lock = threading.RLock()

    def clone(self, *, timeout=None):
        return ClusterConnection(self.config, timeout=self.timeout if timeout is None else timeout)

    def _rpc(self, operation, **params):
        repeatable = {'ping', 'status', 'events', 'worker_list', 'allocation_get', 'allocation_route',
                      'allocation_ack', 'allocation_cancel', 'owner_register', 'owner_heartbeat', 'owner_routes',
                      'pool_status', 'pool_create', 'pool_checkout', 'pool_lease', 'pool_release', 'pool_close',
                      'job_create', 'job_status', 'job_results', 'job_cancel',
                      'snapshot_spec', 'snapshot_info', 'snapshot_verify', 'snapshot_alias'}
        if operation == 'create' and params.get('operation_id'):
            repeatable.add('create')
        try:
            return self.control.call(operation, **params)
        except OperationUnknown:
            if operation not in repeatable:
                raise
            return self.control.call(operation, **params)

    def remember(self, route):
        with self.registry['lock']:
            self.registry['routes'][route['id']] = route
        return route

    def acknowledge_routes(self, routes):
        owners = {}
        for route in routes:
            with self.registry['lock']:
                mine = route.get('owner') in self.registry['owners']
            if mine:
                self.remember(route)
                owners.setdefault(route['owner'], []).append(route['id'])
        for owner, identities in owners.items():
            self._rpc('owner_routes', identity=owner, sandboxes=identities)

    def forget(self, identity):
        with self.registry['lock']:
            self.registry['routes'].pop(identity, None)
        with self.lock:
            for key in list(self.connections):
                if key[0] == identity:
                    self.connections.pop(key).close()

    def _route(self, identity):
        with self.registry['lock']:
            route = self.registry['routes'].get(identity)
        if route is None:
            route = self.remember(self._rpc('allocation_route', identity=identity))
        return route

    def _worker(self, route):
        key = (route['id'], route['endpoint']['port'], route['endpoint']['token'])
        with self.lock:
            if key not in self.connections:
                self.connections[key] = providers.direct(route['endpoint'], timeout=self.timeout)
            return self.connections[key]

    def call(self, operation, **params):
        if operation == 'owner_register':
            result = self._rpc(operation, **params)
            with self.registry['lock']:
                self.registry['owners'][params['identity']] = params['process']
            return result
        if operation == 'owner_heartbeat':
            with self.registry['lock']:
                routes = [r for r in self.registry['routes'].values() if r.get('owner') == params['identity']]
                live = set(self.registry['routes'])
            with self.lock:
                for key in list(self.connections):
                    if key[0] not in live:
                        self.connections.pop(key).close()
            # Renew workers independently before attempting the controller. A
            # controller restart/outage cannot interrupt this heartbeat path.
            seen = set()
            for route in routes:
                key = (route['endpoint']['hostname'], route['endpoint']['port'])
                if key in seen:
                    continue
                try:
                    self._worker(route).call(operation, **params)
                    seen.add(key)
                except Exception:
                    continue
            result = self._rpc(operation, **params)
            current = {r['id'] for r in result.get('routes', [])}
            for route in routes:
                if route['id'] not in current:
                    self.forget(route['id'])
            for route in result.get('routes', []):
                self.remember(route)
            self.acknowledge_routes(result.get('routes', []))
            return result
        if operation == 'create':
            self._rpc(operation, **params)
            deadline = time.monotonic() + params['spec']['startup_timeout']
            try:
                while True:
                    result = self._rpc('allocation_get', identity=params['identity'])
                    if result['state'] == 'ready':
                        route = self.remember(self._rpc('allocation_ack', identity=params['identity']))
                        return route['info']
                    if result['state'] in ('failed', 'terminated', 'stopped'):
                        raise SandboxError(result.get('error') or 'sandbox creation ended: ' + result['state'],
                                           sandbox_id=params['identity'])
                    if time.monotonic() >= deadline:
                        raise TimeoutError('sandbox is waiting: ' + (result.get('reason') or result['state']))
                    time.sleep(.025)
            except BaseException:
                try:
                    self._rpc('allocation_cancel', identity=params['identity'])
                except Exception:
                    pass  # The persisted unacknowledged-request deadline remains.
                raise
        if operation == 'list':
            return [a.get('info') or {'id': a['id'], 'state': a['state']} for a in self._rpc('status')['sandboxes']]
        if operation.startswith(('pool_', 'job_', 'worker_', 'snapshot_', 'allocation_')) or operation in ('ping', 'status', 'events', 'backup', 'shutdown'):
            return self._rpc(operation, **params)
        identity = params.get('identity')
        if identity is None:
            raise ValueError('sandbox operation needs an identity')
        route = self._route(identity)
        params['identity'] = route['id']
        expected = self._rpc('snapshot_alias', key=params['key']) if operation in ('capture', 'stop') and params.get('key') is not None else None
        try:
            result = self._worker(route).call(operation, **params)
        except OperationUnknown:
            # A later call can resolve a new endpoint; never replay a mutation
            # automatically when its first delivery may already have succeeded.
            self.forget(route['id'])
            raise
        if operation in ('capture', 'stop'):
            self.control.call('snapshot_register', identity=route['id'], reference=result['id'],
                              key=params.get('key'), expected=expected)
        if operation in ('terminate', 'stop'):
            try:
                self._rpc('allocation_cancel', identity=route['id'])
            except OperationUnknown:
                pass
            self.forget(route['id'])
        return result

    def close(self):
        self.control.close()
        with self.lock:
            for connection in self.connections.values():
                connection.close()
            self.connections.clear()


class Cluster:
    """Manage a named cluster. Closing this handle leaves its controller running."""
    def __init__(self, config, name=None):
        self.config, self.name = config, name
        self.connection = ClusterConnection(config)

    @dualclassmethod
    def connect(cls, name='lab'):
        config = cluster_config(name)
        if config is None:
            raise ResourceUnavailable('cluster is not configured: ' + str(name))
        return cls(config, name if isinstance(name, str) else None)

    @dualclassmethod
    def start(cls, name='lab', *, directory=None, local_worker=True, slots=None, memory=None):
        if not isinstance(name, str) or name == 'local' or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', name):
            raise ValueError('cluster name must use letters, digits, underscores or hyphens; local is reserved')
        if local_worker and cluster_config(name) is None:
            path = home() / 'config.json'
            if path.exists() and name in json.loads(path.read_text()).get('targets', {}):
                raise FileExistsError('target name already identifies another target: ' + name)
            from ..sandbox.targets import local_connection
            from ..templates.resolve import Template
            # First-use setup can select a new storage directory. Complete it
            # before choosing controller storage and writing its named target.
            prepared = local_connection(template=Template('coding').resolve())
            prepared.close()
        directory = Path(directory or home() / 'clusters' / name).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        config = {'hostname': socket.gethostname(), 'directory': str(directory)}
        # Reserve the name before launching a daemon. A conflicting target must
        # never leave a controller running without a way to address it by name.
        save_target(name, config)
        marker = directory / 'controller.json'
        with locked(directory / 'startup.lock'):
            alive = False
            if marker.exists():
                try:
                    connection = ClusterConnection(config, timeout=2)
                    connection.call('ping')
                    connection.close()
                    alive = True
                except Exception:
                    from ..sandbox.ownership import process_alive
                    previous = json.loads(marker.read_text())
                    if process_alive(previous.get('process')) is not False:
                        raise ResourceUnavailable('controller is alive or its status is uncertain; inspect ' + str(directory / 'controller.log'))
            if not alive:
                environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2]) + os.pathsep + os.environ.get('PYTHONPATH', '')}
                with (directory / 'controller.log').open('ab') as log:
                    child = subprocess.Popen([sys.executable, '-m', 'sandweave.weave.server', '--directory', str(directory)],
                        env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                deadline = time.monotonic() + 30
                while True:
                    if child.poll() is not None:
                        raise ResourceUnavailable('controller failed to start: ' + (directory / 'controller.log').read_text()[-4000:])
                    if marker.exists() and json.loads(marker.read_text()).get('pid') == child.pid:
                        break
                    if time.monotonic() > deadline:
                        raise TimeoutError('controller did not become ready; inspect ' + str(directory / 'controller.log'))
                    time.sleep(.05)
        result = cls(config, name)
        if local_worker and not result.workers:
            from ..sandbox.targets import local_connection
            from ..templates.resolve import Template
            # New installations use the same automatic setup as Sandbox().
            worker = local_connection(template=Template('coding').resolve())
            worker.close()
            save_target(name, config)
            result.add_worker('local', slots=slots, memory=memory)
        return result

    @property
    def info(self):
        return self.connection.call('status')

    @property
    def workers(self):
        return self.connection.call('worker_list')

    @dualmethod
    def add_worker(self, target='local', *, slots=None, memory=None, labels=None, name=None):
        return self.connection.call('worker_add', target=providers.serialize(target), slots=slots,
                                    memory=memory, labels=labels, name=name)

    @dualmethod
    def drain(self, worker):
        return self.connection.call('worker_update', identity=worker, draining=True)

    @dualmethod
    def resume(self, worker):
        return self.connection.call('worker_update', identity=worker, draining=False)

    @dualmethod
    def remove_worker(self, worker):
        return self.connection.call('worker_update', identity=worker, remove=True)

    @dualmethod
    def events(self, *, after=0, limit=100):
        return self.connection.call('events', after=after, limit=limit)

    @dualmethod
    def backup(self, destination):
        return self.connection.call('backup', destination=str(destination))

    @dualmethod
    def stop(self):
        return self.connection.call('shutdown')

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _after_fork():
    global _registries, _registry_lock
    _registries, _registry_lock = {}, threading.Lock()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork)

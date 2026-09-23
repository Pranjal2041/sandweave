"""Persistent, worker-local service networks independent of sandbox templates."""
import ipaddress
import json
import os
from pathlib import Path
import re
import threading
import uuid

from .errors import ResourceUnavailable, UnsupportedFeature, OperationUnknown
from .workspace import atomic_json, home


def network_id(identity):
    if not isinstance(identity, str) or not re.fullmatch(r'net-[a-f0-9]{32}', identity):
        raise ValueError('invalid service network ID')
    return identity


def membership(identity, aliases=None, networks=None):
    network_id(identity)
    def names(values, label):
        if isinstance(values, str) or not isinstance(values, (list, tuple)):
            raise ValueError(label + ' must be a list of names')
        result = []
        for value in values:
            if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}', value):
                raise ValueError('invalid ' + label + ' name')
            value = value.lower()
            if value not in result:
                result.append(value)
        return result
    selected = names(['default'] if networks is None else networks, 'networks')
    if not selected:
        raise ValueError('service network membership needs at least one named network')
    return {'id': identity, 'aliases': names([] if aliases is None else aliases, 'aliases'), 'networks': selected}


def _call(operation, identity, target):
    from .targets import connect
    from ..templates.resolve import Template
    connection = connect(target, template=Template('coding').resolve())
    try:
        if not connection.call('ping').get('service_networks'):
            raise UnsupportedFeature('service networks require Sandweave 0.2.24 or newer on the controller and worker')
        try:
            return connection.call(operation, identity=identity)
        except OperationUnknown:
            # The ID is chosen before sending. Creation and deletion are
            # idempotent, including after a lost acknowledgement.
            return connection.call(operation, identity=identity)
    finally:
        connection.close()


def create_service_network(*, target=None):
    """Create a private service network on a target and return its opaque ID."""
    identity = 'net-' + uuid.uuid4().hex
    _call('service_network_create', identity, target)
    return identity


def delete_service_network(identity, *, target=None):
    """Delete an empty network. Terminate its sandboxes before deleting it."""
    return _call('service_network_delete', network_id(identity), target)


class Networks:
    """The worker hub owns membership; template workspaces share its authority.

    Preparing a desktop can create another immutable workspace in the same
    worker allocation. Its control plane registers with the original hub;
    packets never cross an extra RPC hop or leave this host.
    """
    def __init__(self, worker, hub):
        from .ownership import process_scope
        self.worker, self.hub = worker, hub
        self.root = worker.root / 'service-networks'
        self.root.mkdir(exist_ok=True, mode=0o700)
        self.index = home() / 'service-networks'
        self.scope = {'process': process_scope(), 'cpus': sorted(os.sched_getaffinity(0)),
                      'cgroup': Path('/proc/self/cgroup').read_text(),
                      'allocation': {key: os.environ.get(key) for key in
                                     ('SLURM_JOB_ID', 'SLURM_STEP_ID', 'SLURM_MEM_PER_NODE',
                                      'SLURM_MEM_PER_CPU', 'SANDWEAVE_MEMORY_BUDGET')},
                      'gpu': {key: os.environ.get(key) for key in
                              ('CUDA_VISIBLE_DEVICES', 'NVIDIA_VISIBLE_DEVICES', 'SANDWEAVE_GPU_DEVICES',
                               'SANDWEAVE_GPU_LIMIT', 'SLURM_STEP_GPUS', 'SLURM_JOB_GPUS')}}
        self.guard, self.locks = threading.Lock(), {}
        for path in self.root.glob('net-*.json'):
            record = json.loads(path.read_text())
            if record['state'] != 'ready':
                continue
            self.hub.register(record['id'], [])
            for member in record['members'].values():
                self.hub.add(record['id'], member)

    def lock(self, identity):
        with self.guard:
            return self.locks.setdefault(identity, threading.RLock())

    def path(self, identity):
        return self.root / (network_id(identity) + '.json')

    def read(self, identity):
        return json.loads(self.path(identity).read_text())

    @staticmethod
    def public(record):
        return {'id': record['id'], 'state': record['state'], 'members': list(record['members'])}

    def dispatch(self, operation, identity, **parameters):
        network_id(identity)
        local = self.path(identity)
        index = self.index / (identity + '.json')
        if not local.exists() and index.exists():
            owner = json.loads(index.read_text())
            if owner['scope'] != self.scope:
                raise ResourceUnavailable('service network belongs to a different worker allocation')
            metadata = json.loads(Path(owner['metadata']).read_text())
            if metadata.get('workspace') != owner['workspace'] or metadata.get('status') != 'ready':
                raise ResourceUnavailable('service network worker is unavailable')
            from .connection import Connection
            connection = Connection('127.0.0.1', metadata['port'], metadata['token'], timeout=10)
            try:
                return connection.call(operation, identity=identity, **parameters)
            finally:
                connection.close()
        with self.lock(identity):
            if operation == 'service_network_create':
                if local.exists():
                    record = self.read(identity)
                    if record['state'] != 'ready':
                        raise FileNotFoundError('service network was deleted')
                else:
                    record = {'id': identity, 'state': 'ready', 'next_address': 2, 'members': {}}
                    atomic_json(local, record)
                    self.hub.register(identity, [])
                atomic_json(index, {'workspace': str(self.worker.root), 'scope': self.scope,
                                    'metadata': str(self.worker.metadata_path)})
                return self.public(record)
            record = self.read(identity)
            if operation == 'service_network_delete':
                if record['members']:
                    raise ResourceUnavailable('terminate the service network members before deleting the network')
                record['state'] = 'deleted'
                atomic_json(local, record)
                self.hub.unregister(identity)
                return self.public(record)
            if operation == 'service_network_leave':
                member = record['members'].pop(parameters['member'], None)
                if member is not None:
                    atomic_json(local, record)
                    self.hub.remove(identity, member['address'])
                return self.public(record)
            if record['state'] != 'ready':
                raise FileNotFoundError('service network was deleted')
            if operation == 'service_network_join':
                entry = parameters['entry']
                previous = record['members'].get(entry['identity'])
                if previous is not None:
                    if any(previous.get(key) != value for key, value in entry.items()):
                        raise ValueError('sandbox already has different network membership')
                    return {**previous, 'hub': str(self.hub.path), 'dynamic': True}
                offset = record['next_address']
                base = int(ipaddress.IPv4Address('10.231.0.0'))
                used = {item['address'] for item in record['members'].values()}
                # Recycle only after traversing the address space, so a long-
                # lived network is bounded by live members, not total launches.
                for _ in range(65533):
                    offset = 2 if offset >= 65535 else offset
                    address = str(ipaddress.IPv4Address(base + offset))
                    offset += 1
                    if address not in used:
                        break
                else:
                    raise ResourceUnavailable('service network has no free member addresses')
                entry = {**entry, 'address': address}
                self.hub.add(identity, entry)
                record['members'][entry['identity']] = entry
                record['next_address'] = offset
                try:
                    atomic_json(local, record)
                except BaseException:
                    self.hub.remove(identity, entry['address'])
                    raise
                return {**entry, 'hub': str(self.hub.path), 'dynamic': True}
            if operation == 'service_network_info':
                return self.public(record)
            raise UnsupportedFeature('unknown service network operation')

    def join(self, record):
        spec = record['spec']
        member = membership(spec['service_network']['id'], spec['service_network'].get('aliases'),
                            spec['service_network'].get('networks'))
        if spec.get('services') or spec.get('service_group') or spec.get('_service_network'):
            raise UnsupportedFeature('an existing service group cannot join another service network')
        if spec['runtime'] != 'gvisor':
            raise UnsupportedFeature('service networks require the gVisor runtime')
        entry = {'identity': record['id'], 'networks': member['networks'],
                 'aliases': member['aliases'],
                 'socket': str(self.worker.services.local / 'gvisor/network' / record['id'] / 'mesh.sock')}
        route = self.dispatch('service_network_join', member['id'], entry=entry)
        spec['_service_network'] = route
        self.worker.write(record)

    def leave(self, record):
        member = record['spec'].get('service_network', {})
        if member.get('id'):
            self.dispatch('service_network_leave', member['id'], member=record['id'])

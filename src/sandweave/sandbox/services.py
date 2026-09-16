"""Native service groups share placement and admission, with separate sandboxes."""
import atexit
import copy
from concurrent.futures import ThreadPoolExecutor
import ipaddress
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import uuid

from .errors import UnsupportedFeature


class ServiceConnection:
    def __init__(self, connection, identity, service):
        self.connection, self.identity, self.service = connection, identity, service

    def clone(self, **options):
        return ServiceConnection(self.connection.clone(**options), self.identity, self.service)

    def call(self, operation, **parameters):
        return self.connection.call('service_rpc', identity=self.identity, service=self.service,
                                    method=operation, parameters=parameters)

    def close(self):
        self.connection.close()


def view(sandbox, service):
    from .sandbox import Sandbox
    from .files import Files
    result = Sandbox.__new__(Sandbox)
    result._connection = ServiceConnection(sandbox._connection.clone(), sandbox.id, service)
    try:
        result._info = result._connection.call('describe')
        result.id = result._info['id']
        result._owned, result._closed, result._terminated = False, False, False
        result._controls, result._target = {}, sandbox._target
        result.files = Files(result)
        return result
    except BaseException:
        result._connection.close()
        raise


class Services:
    def __init__(self, worker):
        from service_network import Hub
        self.worker = worker
        self.local = Path((worker.root / 'runs/local-path.txt').read_text().strip())
        self.hub = Hub(self.local / 'service-networks')
        atexit.register(self.hub.close)
        for path in worker.records.glob('*.bin'):
            record = worker.read(path.stem)
            if record.get('services') and not record.get('cleanup_complete') and record['state'] not in ('terminated', 'stopped'):
                self.hub.register(record['id'], record['services'].values())

    def prepare(self, record):
        definitions = record['spec']['services']
        if not isinstance(definitions, dict) or len(definitions) > 65000:
            raise ValueError('invalid service group')
        for name, service in definitions.items():
            if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}', name) or name == 'main':
                raise ValueError('invalid service name: ' + name)
            if service['request']['spec'].get('services'):
                raise ValueError('service groups cannot be nested')
            if service['request']['spec']['runtime'] != 'gvisor':
                raise UnsupportedFeature('service groups require the gVisor runtime')
        spec = copy.deepcopy(record['spec'])
        for name, settings in spec.get('service_volumes', {}).items():
            if settings.get('exclusive'):
                users = [member for member, mounts in [
                    ('main', spec.get('service_mounts', [])),
                    *((member, definition.get('volumes', [])) for member, definition in definitions.items())]
                    for mount in mounts if mount['name'] == name]
                if len(users) != 1:
                    raise ValueError('an exclusive volume must have exactly one mount')
        # Persist group ownership before allocating anything. Failed preparation
        # and worker recovery use the same idempotent cleanup path as termination.
        record['service_group'] = True
        self.worker.write(record)
        volume_root = self.local / 'service-data' / record['id']
        record['service_data_root'] = str(volume_root)
        self.worker.write(record)
        volumes = {}
        for name, settings in spec.get('service_volumes', {}).items():
            if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', name):
                raise ValueError('invalid service volume name')
            path = volume_root / name
            path.mkdir(parents=True)
            (path / 'owners').mkdir(mode=0o700)
            if settings.get('file'):
                (path / 'data').touch(exist_ok=False)
            else:
                (path / 'data').mkdir(mode=0o777)
                (path / 'data').chmod(0o777)
            volumes[name] = str(path)
        services = {'main': {'identity': record['id'], **spec.get('service_network', {})}}
        for name, service in definitions.items():
            services[name] = {'identity': 'sw-' + uuid.uuid4().hex,
                              'networks': service.get('networks', ['default']),
                              'aliases': service.get('aliases', [name]),
                              'network_aliases': service.get('network_aliases')}
        for index, (name, service) in enumerate(services.items()):
            service.setdefault('networks', ['default'])
            service.setdefault('aliases', [name])
            service['address'] = str(ipaddress.IPv4Address(int(ipaddress.IPv4Address('10.231.0.2')) + index))
            service['socket'] = str(self.local / 'gvisor/network' / service['identity'] / 'mesh.sock')
        for name, service in services.items():
            peers = {}
            for other in services.values():
                common = set(service['networks']).intersection(other['networks'])
                if common:
                    aliases = (set().union(*(other['network_aliases'].get(network, []) for network in common))
                               if other.get('network_aliases') else other['aliases'])
                    for alias in aliases:
                        peers[alias] = other['address']
            network = {**service, 'hub': str(self.hub.path), 'peers': peers}
            if name == 'main':
                spec['_service_network'] = network
                current, mounts = spec, spec.get('service_mounts', [])
            else:
                definition = spec['services'][name]
                current, mounts = definition['request']['spec'], definition.get('volumes', [])
                current['_service_network'] = network
            staged = set()
            for mount in mounts:
                destination = mount.get('staging', mount['target'])
                if destination in staged:
                    continue
                staged.add(destination)
                current['mounts'].append({'source': volumes[mount['name']], 'destination': mount.get('staging', mount['target']),
                    'read_only': mount.get('read_only', False) and 'staging' not in mount,
                    'snapshot': 'reject', '_private_volume': True,
                    **({'_exclusive': True} if spec['service_volumes'][mount['name']].get('exclusive') else {})})
        record['spec'], record['services'], record['service_volumes'] = spec, services, volumes
        self.worker.write(record)
        self.hub.register(record['id'], services.values())
        return spec

    def launch(self, record):
        if not record['spec']['services']:
            return
        def create(item):
            name, definition = item
            identity = record['services'][name]['identity']
            request = copy.deepcopy(definition['request'])
            spec = request['spec']
            spec['detached'] = record['owner'] is None
            spec['startup_timeout'] = self.worker.remaining(record['id'])
            if spec.get('image') and request.get('reference') is None:
                from ..templates.images import configure
                spec, _ = configure(spec, self.worker.root, refresh=request.get('refresh', False))
            spec['startup_timeout'] = self.worker.remaining(record['id'])
            self.worker._create(spec, identity, reference=request.get('reference'),
                                operation_id=uuid.uuid4().hex, owner=record['owner'], service_parent=record['id'])
        with ThreadPoolExecutor(max_workers=min(16, len(record['spec']['services'])),
                                thread_name_prefix='sandweave-service-start') as executor:
            results = list(executor.map(create, record['spec']['services'].items()))
        return results

    def close(self, record):
        errors = []
        for name, service in record.get('services', {}).items():
            if name == 'main' or not self.worker.path(service['identity']).exists():
                continue
            try:
                with self.worker.lock(service['identity']):
                    self.worker.terminate(service['identity'])
            except Exception as error:
                errors.append(error)
        self.hub.unregister(record['id'])
        if errors:
            raise ExceptionGroup('service group cleanup failed', errors)

    def discard(self, record):
        directory = Path(record.get('service_data_root', self.worker.root / 'service-data' / record['id']))
        if directory.exists():
            shutil.rmtree(directory)

    def import_volume(self, identity, name, path):
        record = self.worker.read(identity)
        if record['spec']['service_volumes'][name].get('exclusive'):
            raise ValueError('exclusive volumes are written only by their owning sandbox')
        destination = Path(record.get('service_volumes', {})[name]) / 'data'
        agent = self.worker.agent(identity)
        handle = uuid.uuid4().hex
        agent.call('file', op='open', handle=handle, path=path, mode='rb')
        try:
            with tempfile.TemporaryFile() as stream:
                while chunk := agent.call('file', op='read', handle=handle, size=1024**2):
                    stream.write(chunk)
                stream.seek(0)
                if destination.is_file():
                    with destination.open('wb') as output:
                        shutil.copyfileobj(stream, output)
                else:
                    with tarfile.open(fileobj=stream) as archive:
                        archive.extractall(destination, filter='data')
        finally:
            agent.call('file', op='close', handle=handle)
        return {'ready': True}

    def dispatch(self, identity, service, method, parameters):
        allowed = {'describe', 'command_start', 'process_status', 'process_output', 'process_stdin',
                   'process_terminate', 'process_resize', 'file', 'terminate', 'network_policy', 'service_setup'}
        if method not in allowed:
            raise UnsupportedFeature('unsupported service operation: ' + method)
        record = self.worker.read(identity)
        if service not in record.get('services', {}):
            raise ValueError('unknown service: ' + service)
        child = record['services'][service]['identity']
        return self.worker.dispatch(method, {**parameters, 'identity': child})

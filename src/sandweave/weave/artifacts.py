"""Snapshot discovery and transfer through existing authenticated worker RPCs."""
from ..sandbox.errors import CacheMiss, CacheConflict, OperationUnknown, ResourceUnavailable
from ..sandbox.transfers import transfer


def register(controller, reference, endpoint, key=None, *, expected=None, compare=False):
    connection = controller.connection(endpoint)
    try:
        spec = connection.call('snapshot_spec', reference=reference)
        info = connection.call('snapshot_info', reference=spec['reference'])
    finally:
        connection.close()
    with controller.state.transaction():
        controller.connections.check(endpoint)
        old = controller.state.get('artifact', info['id'], required=False)
        if old and (old.get('retiring') or old.get('released')):
            raise CacheMiss('snapshot is being released: ' + info['id'])
        locations = old['locations'] if old else []
        if endpoint not in locations:
            locations.append(endpoint)
        controller.state.put('artifact', dict(id=info['id'], info=info, spec=spec, locations=locations))
        if key is not None:
            current = controller.state.get('alias', key, required=False)
            if compare and (current or {}).get('reference') != expected:
                raise CacheConflict('cluster cache name changed during capture: ' + key)
            controller.state.put('alias', dict(id=key, reference=info['id']))
    return spec


def resolve(controller, reference):
    alias = controller.state.get('alias', reference, required=False)
    identity = alias['reference'] if alias else reference
    record = controller.state.get('artifact', identity, required=False)
    if record:
        if record.get('retiring') or record.get('released'):
            raise CacheMiss('snapshot has been released or is being released: ' + identity)
        return record
    for worker in controller.state.list('worker', state='ready'):
        try:
            spec = register(controller, reference, worker['endpoint'], key=reference if not reference.startswith('snap-') else None)
            return controller.state.get('artifact', spec['reference'])
        except (CacheMiss, OperationUnknown, ResourceUnavailable, OSError):
            continue
    raise CacheMiss('cluster has no saved revision or cache: ' + reference)


def ensure(controller, reference, destination, endpoint, shared_cache=None):
    controller.connections.check(endpoint)
    record = resolve(controller, reference)
    try:
        destination.call('snapshot_info', reference=record['id'])
        if endpoint not in record['locations']:
            register(controller, record['id'], endpoint)
        return record['id']
    except (CacheMiss, FileNotFoundError):
        pass
    try:
        if shared_cache is not None and destination.call('artifact_cached', reference=record['id'], shared_cache=shared_cache)['ready']:
            register(controller, record['id'], endpoint)
            return record['id']
    except (CacheMiss, FileNotFoundError):
        pass
    errors = []
    for location in record['locations']:
        if not controller.connections.available(location):
            continue
        source = None
        try:
            source = controller.connection(location)
            transfer(source, destination, record['id'], shared_cache=shared_cache,
                     lock_root=controller.state.root / 'transfers')
            register(controller, record['id'], endpoint)
            return record['id']
        except Exception as error:
            errors.append(str(error))
        finally:
            if source:
                source.close()
    controller.connections.check(endpoint)
    raise CacheMiss('snapshot sources are unavailable: ' + '; '.join(errors))


def dispatch(controller, operation, parameters):
    if operation == 'snapshot_alias':
        record = controller.state.get('alias', parameters['key'], required=False)
        return (record or {}).get('reference')
    if operation == 'snapshot_register':
        allocation = controller._resolve(parameters['identity'])
        return register(controller, parameters['reference'], allocation['endpoint'], parameters.get('key'),
                        expected=parameters.get('expected'), compare='expected' in parameters)
    record = resolve(controller, parameters['reference'])
    if operation == 'snapshot_spec':
        return record['spec']
    errors = []
    for endpoint in record['locations']:
        if not controller.connections.available(endpoint):
            continue
        connection = None
        try:
            connection = controller.connection(endpoint)
            return connection.call(operation, reference=record['id'])
        except Exception as error:
            errors.append(str(error))
        finally:
            if connection:
                connection.close()
    raise CacheMiss('snapshot sources are unavailable: ' + '; '.join(errors))

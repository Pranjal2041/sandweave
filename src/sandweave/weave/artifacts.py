"""Snapshot discovery and transfer through existing authenticated worker RPCs."""
from . import providers
from ..sandbox.errors import CacheMiss, CacheConflict, OperationUnknown, ResourceUnavailable


def register(controller, reference, endpoint, key=None, *, expected=None, compare=False):
    connection = controller.connection(endpoint)
    try:
        spec = connection.call('snapshot_spec', reference=reference)
        info = connection.call('snapshot_info', reference=spec['reference'])
    finally:
        connection.close()
    with controller.state.transaction():
        old = controller.state.get('artifact', info['id'], required=False)
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
        return record
    for worker in controller.state.list('worker', state='ready'):
        try:
            spec = register(controller, reference, worker['endpoint'], key=reference if not reference.startswith('snap-') else None)
            return controller.state.get('artifact', spec['reference'])
        except (CacheMiss, OperationUnknown, ResourceUnavailable, OSError):
            continue
    raise CacheMiss('cluster has no saved revision or cache: ' + reference)


def ensure(controller, reference, destination, endpoint):
    record = resolve(controller, reference)
    try:
        destination.call('snapshot_info', reference=record['id'])
        return record['id']
    except (CacheMiss, FileNotFoundError):
        pass
    errors = []
    for location in record['locations']:
        source = None
        try:
            source = controller.connection(location)
            metadata = source.call('artifact_metadata', reference=record['id'])
            try:
                destination.call('artifact_import', metadata=metadata)
            except FileNotFoundError:
                manifest = source.call('artifact_manifest', reference=record['id'])
                missing = destination.call('artifact_begin', manifest=manifest)
                for path in missing:
                    size = manifest['files'][path]['size']
                    for offset in range(0, size, 1024**2):
                        data = source.call('artifact_read', reference=record['id'], path=path,
                                           offset=offset, size=min(1024**2, size-offset))
                        if not data:
                            raise OSError('artifact source returned an incomplete file: ' + path)
                        destination.call('artifact_write', reference=record['id'], path=path,
                                         offset=offset, data=data)
                destination.call('artifact_finish', reference=record['id'])
            register(controller, record['id'], endpoint)
            return record['id']
        except Exception as error:
            errors.append(str(error))
        finally:
            if source:
                source.close()
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

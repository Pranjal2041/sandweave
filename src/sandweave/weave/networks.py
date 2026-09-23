"""Pin public service networks to a worker allocation, without reserving slots."""
from .lifecycle import lifecycle, rpc
from ..sandbox.errors import ResourceUnavailable, UnsupportedFeature
from ..sandbox.networks import network_id


@lifecycle
def dispatch(controller, operation, identity):
    network_id(identity)
    state = controller.state
    with state.transaction():
        record = state.get('network', identity, required=False)
        if operation == 'service_network_create':
            if record is None:
                workers = [worker for worker in state.list('worker')
                           if worker['state'] == 'ready' and not worker.get('draining') and not worker.get('lost')]
                if not workers:
                    raise ResourceUnavailable('no ready worker is available for the service network')
                counts = {}
                for network in state.list('network'):
                    if network['state'] != 'deleted':
                        counts[network['worker']] = counts.get(network['worker'], 0) + 1
                worker = min(workers, key=lambda item: (counts.get(item['id'], 0), item['id']))
                record = state.put('network', {'id': identity, 'worker': worker['id'],
                                               'endpoint': worker['endpoint'], 'state': 'creating'})
            elif record['state'] == 'ready':
                return {'id': identity, 'worker': record['worker'], 'state': 'ready'}
            elif record['state'] == 'failed':
                record = state.put('network', {**record, 'state': 'creating'})
            elif record['state'] != 'creating':
                raise FileNotFoundError('service network was deleted or is being deleted')
        elif operation == 'service_network_delete':
            if record is None:
                raise FileNotFoundError('unknown service network')
            if record['state'] == 'deleted':
                return {'id': identity, 'state': 'deleted'}
            if record['state'] == 'creating':
                raise ResourceUnavailable('service network creation is still in progress')
            members = state.list('allocation', released=False, fields=('spec.service_network',))
            if any((member['spec.service_network'] or {}).get('id') == identity for member in members):
                raise ResourceUnavailable('terminate the service network members before deleting the network')
            record = state.put('network', {**record, 'state': 'deleting'})
        else:
            raise UnsupportedFeature('unknown service network operation')
    worker = state.get('worker', record['worker'])
    if worker.get('lost') or worker['state'] == 'removed':
        if operation == 'service_network_delete':
            # This is a controller tombstone, not a claim about dead host data.
            state.put('network', {**record, 'state': 'deleted'})
            return {'id': identity, 'state': 'deleted'}
        raise ResourceUnavailable('service network worker is unavailable')
    connection = controller.connection(record['endpoint'])
    try:
        if not (yield rpc(connection, 'ping')).get('service_networks'):
            raise UnsupportedFeature('service networks require Sandweave 0.2.24 or newer on the worker')
        result = yield rpc(connection, operation, identity=identity)
    except Exception as error:
        with state.transaction():
            current = state.get('network', identity)
            expected = 'deleting' if operation == 'service_network_delete' else 'creating'
            if current['state'] == expected:
                state.put('network', {**current, 'state': 'ready' if expected == 'deleting' else 'failed',
                                      'error': str(error)})
        raise
    finally:
        connection.close()
    with state.transaction():
        current = state.get('network', identity)
        state.put('network', {**current, 'state': result['state']})
    return {'id': identity, 'worker': record['worker'], 'state': result['state']}


def placement(controller, spec):
    member = spec.get('service_network', {})
    if not member.get('id'):
        return {}
    record = controller.state.get('network', network_id(member['id']))
    if record['state'] != 'ready':
        raise ResourceUnavailable('service network is not ready: ' + record['state'])
    worker = controller.state.get('worker', record['worker'])
    if worker.get('lost') or worker['state'] == 'removed':
        raise ResourceUnavailable('service network worker was lost; create a new network')
    return {'only_worker': record['worker']}

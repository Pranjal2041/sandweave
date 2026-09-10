"""Existing capacity and replaceable machine providers."""
import socket

from ...sandbox.connection import Connection
from ...sandbox.targets import Endpoint, Slurm, connect, _tunnel


def serialize(target):
    if isinstance(target, Slurm):
        return {'job_id': target.job_id, **target.config}
    if isinstance(target, Endpoint):
        return {'endpoint': {'hostname': socket.gethostname(), 'port': target.port, 'token': target.token}}
    if target is None:
        return 'local'
    if not isinstance(target, (str, dict)):
        raise TypeError('worker target must be local, an SSH target, or a Slurm allocation')
    return target


def endpoint(information, connection, target=None):
    """Publish the actual worker endpoint, never a controller-local tunnel port."""
    value = {'hostname': information['hostname'], 'port': information['port'], 'token': connection.token}
    if information.get('workspace'):
        value['workspace'] = information['workspace']
    if isinstance(target, dict) and target.get('endpoint', {}).get('relay'):
        value['relay'] = target['endpoint']['relay']
    if isinstance(target, dict) and target.get('host'):
        value['ssh_host'] = target['host']
    elif isinstance(target, str) and target.startswith('ssh://'):
        value['ssh_host'] = target[6:]
    return value


def direct(value, *, token=None, timeout=300):
    hostname, port = value['hostname'], value['port']
    if hostname != socket.gethostname():
        port = (_tunnel(value.get('ssh_host', hostname), port, ssh_port=value['ssh_port'])
                if value.get('ssh_port') else _tunnel(value.get('ssh_host', hostname), port))
    return Connection('127.0.0.1', port, token or value['token'], timeout=timeout)


def attach(target, *, template=None):
    target = serialize(target)
    if isinstance(target, dict) and 'endpoint' in target:
        return direct(target['endpoint'])
    return connect(target, template=template)

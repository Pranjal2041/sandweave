"""Opt-in process visibility and bounded runtime audit exports."""
import asyncio
import os
from pathlib import Path

from .errors import UnsupportedFeature


def configure(recipe, runtime, *, hidepid=None, audit=None):
    options = recipe.setdefault('runtime_options', {})
    for name, value in (('hidepid', hidepid), ('audit', audit)):
        if value is not None:
            options[name] = value
    if type(options.get('hidepid', 0)) is not int or options.get('hidepid', 0) not in (0, 1, 2):
        raise ValueError('hidepid must be 0, 1 or 2')
    if type(options.get('audit', False)) is not bool:
        raise ValueError('audit must be a bool')
    limit = options.get('audit_max_bytes', 1024**3)
    if type(limit) is not int or not 1 <= limit <= 2**50:
        raise ValueError('audit_max_bytes must be a positive integer at most 2**50')
    if (options.get('hidepid') or options.get('audit')) and runtime != 'gvisor':
        raise UnsupportedFeature('hidepid and audit require runtime="gvisor"')


def enabled(spec):
    options = spec['template'].get('runtime_options', {})
    return bool(options.get('hidepid') or options.get('audit'))


def check_worker(spec, connection):
    if enabled(spec) and not connection.call('ping').get('proc_audit'):
        raise UnsupportedFeature('hidepid and audit require updated Sandweave workers and controller')


def finish(path, status):
    result = {**status, 'path': str(path)}
    from .workspace import atomic_json
    atomic_json(path.with_name(path.name + '.status.json'), result)
    return result


def open_log(path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, 'wb')
    except BaseException:
        os.close(descriptor)
        raise


def export(env, path):
    status = env._call('runtime_audit')
    path = Path(path)
    finish(path, {'complete': False, 'error': 'export has not completed'})
    offset = 0
    with open_log(path) as output:
        while offset < status['bytes']:
            data = env._call('runtime_audit', offset=offset, size=min(1024**2, status['bytes']-offset))
            if not data:
                raise RuntimeError('audit export ended before its recorded boundary')
            output.write(data)
            offset += len(data)
    return finish(path, status)


async def export_async(env, path):
    status = await env._acall('runtime_audit')
    path = Path(path)
    finish(path, {'complete': False, 'error': 'export has not completed'})
    offset = 0
    with open_log(path) as output:
        while offset < status['bytes']:
            data = await env._acall('runtime_audit', offset=offset, size=min(1024**2, status['bytes']-offset))
            if not data:
                raise RuntimeError('audit export ended before its recorded boundary')
            await asyncio.to_thread(output.write, data)
            offset += len(data)
    return await asyncio.to_thread(finish, path, status)

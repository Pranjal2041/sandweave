"""Validated external bind mounts shared by the launcher and SDK adapters."""
import json
from pathlib import Path, PurePosixPath


def normalize(values):
    result, destinations = [], []
    for value in values:
        if set(value) - {'source', 'destination', 'read_only', 'snapshot', '_private_volume', '_exclusive'}:
            raise ValueError('unknown mount fields')
        source = Path(value['source'])
        destination = PurePosixPath(value['destination'])
        if not source.is_absolute() or not source.exists():
            raise ValueError('mount source must exist at an absolute path on the worker')
        if not destination.is_absolute() or '..' in destination.parts or str(destination) == '/':
            raise ValueError('mount destination must be an absolute guest path below /')
        if any(c in str(source) + str(destination) for c in (':', ',', '\n', '\0')):
            raise ValueError('mount paths cannot contain bind-option delimiters')
        if any(destination == other or destination in other.parents or other in destination.parents for other in destinations):
            raise ValueError('mount destinations cannot overlap')
        destinations.append(destination)
        read_only = value.get('read_only', True)
        if type(read_only) is not bool:
            raise ValueError('mount read_only must be a boolean')
        policy = value.get('snapshot', 'rebind' if read_only else 'reject')
        if policy not in ('rebind', 'reject'):
            raise ValueError('external mount snapshots must explicitly rebind or reject capture')
        result.append({'source': str(source.resolve()), 'destination': str(destination),
                       'read_only': read_only, 'snapshot': policy})
        if value.get('_private_volume'):
            if not (source / 'data').exists() or not (source / 'owners').is_dir():
                raise ValueError('private volume wrapper is incomplete')
            result[-1]['_private_volume'] = True
        if value.get('_exclusive'):
            if not value.get('_private_volume'):
                raise ValueError('exclusive access requires an owned private volume')
            result[-1]['_exclusive'] = True
    return result


def configure(spec, values):
    previous = json.loads(spec.get('annotations', {}).get('dev.sandweave.external-mounts', '[]'))
    for value in previous:
        annotations = spec.get('annotations', {})
        annotations.pop('dev.sandweave.private-volume.' + value['destination'], None)
        annotations.pop('dev.sandweave.private-volume-exclusive.' + value['destination'], None)
    remove = {v['destination'] for v in previous}
    spec['mounts'] = [m for m in spec['mounts'] if m['destination'] not in remove]
    occupied = {m['destination'] for m in spec['mounts']}
    for index, value in enumerate(values):
        destination = value['destination']
        if destination in occupied or any(destination == p or destination.startswith(p + '/') for p in
                                          ('/proc', '/sys', '/dev', '/opt/engine-gpu', '/run', '/var/lib/sandweave')):
            raise ValueError('mount conflicts with runtime infrastructure: ' + destination)
        spec['mounts'].append({'source': '/external/' + str(index), 'destination': destination,
                               'type': 'bind', 'options': ['bind', 'ro' if value['read_only'] else 'rw']})
        if value.get('_private_volume'):
            spec.setdefault('annotations', {})['dev.sandweave.private-volume.' + destination] = 'true'
        if value.get('_exclusive'):
            spec.setdefault('annotations', {})['dev.sandweave.private-volume-exclusive.' + destination] = 'true'
    spec.setdefault('annotations', {})['dev.sandweave.external-mounts'] = json.dumps(values)


if __name__ == '__main__':
    import sys
    for index, value in enumerate(normalize(json.loads(Path(sys.argv[1]).read_text()))):
        sys.stdout.buffer.write((value['source'] + ':/external/' + str(index) +
                                 (':ro' if value['read_only'] else ':rw')).encode() + b'\0')

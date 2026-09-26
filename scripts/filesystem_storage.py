"""Private disk backing for writable files, independent of guest RAM backing."""
import atexit
import copy
import json
from pathlib import Path
import shutil
import tempfile

PREFIX = '/sandbox-storage'
ANNOTATION = 'dev.sandweave.storage.mounts'


def persistent_mounts(spec):
    """Return original tmpfs declarations, including disk-backed mount hints."""
    saved = json.loads(spec.get('annotations', {}).get(ANNOTATION, '[]'))
    originals = {m['destination']: m for m in saved}
    return [originals.get(m['destination'], m) for m in spec['mounts']]


def configure(spec, mode, parent, logs):
    annotations = spec.setdefault('annotations', {})
    spec['mounts'] = persistent_mounts(spec)
    annotations.pop(ANNOTATION, None)
    for key in list(annotations):
        if key.startswith('dev.gvisor.spec.mount.sandweave-storage-'):
            del annotations[key]
    if mode == 'memory':
        annotations['dev.gvisor.spec.rootfs.overlay'] = 'memory'
        (logs / 'storage.json').write_text(json.dumps({'mode': mode, 'directory': None}))
        return None
    parent = Path(parent).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='sandweave-storage-', dir=parent))
    marker = directory / '.sandweave-owner'
    marker.write_text(str(logs.resolve()))
    (logs / 'storage.json').write_text(json.dumps({'mode': mode, 'directory': str(directory)}))
    atexit.register(cleanup, logs)
    annotations['dev.gvisor.spec.rootfs.overlay'] = 'dir=' + PREFIX
    originals = []
    for mount in spec['mounts']:
        destination = mount['destination']
        if mount['type'] != 'tmpfs' or any(destination == p or destination.startswith(p + '/')
                for p in ('/dev', '/proc', '/sys', '/run', '/tmp')):
            continue
        index = len(originals)
        originals.append(copy.deepcopy(mount))
        name = f'mount-{index}'
        (directory / name).mkdir()
        source = PREFIX + '/' + name
        hint = f'dev.gvisor.spec.mount.sandweave-storage-{index}.'
        annotations.update({hint + 'source': source, hint + 'type': 'tmpfs',
                            hint + 'share': 'container', hint + 'options': ','.join(mount.get('options', []))})
        mount.update(type='bind', source=source)
    annotations[ANNOTATION] = json.dumps(originals)
    return directory


def cleanup(logs):
    """Called only after this launcher's runtime has stopped."""
    logs = Path(logs)
    try:
        record = json.loads((logs / 'storage.json').read_text())
        if record['mode'] != 'disk':
            return
        directory = Path(record['directory'])
        if not directory.name.startswith('sandweave-storage-'):
            raise ValueError('invalid writable storage ownership')
        if (directory / '.sandweave-owner').read_text() != str(logs.resolve()):
            raise ValueError('writable storage belongs to another sandbox')
    except FileNotFoundError:
        return
    shutil.rmtree(directory)

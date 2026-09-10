"""Prepare immutable image bases once, then reuse them across worker launches."""
import copy
import io
import json
import os
from pathlib import Path
import struct
import tarfile
import tempfile
import time

from .layers import Filesystem, unpack
from .registry import Registry, reference
from .resolve import fingerprint
from ..sandbox.workspace import atomic_json, file_digest, file_signature, home, locked, _immutable

PYTHON_NAME = 'cpython-3.13.15+20260901-x86_64-unknown-linux-musl-lto+static-full.tar.zst'
PYTHON_URL = ('https://github.com/astral-sh/python-build-standalone/releases/download/20260901/' +
              PYTHON_NAME.replace('+', '%2B'))
PYTHON_SHA256 = '81937f0eb62b3c8440543ffb0e3453d5ef3daf671c5344d4acc41207535ed84d'
PRIVATE = '.sandweave-runtime'
FORMAT = 1


def validate(image):
    reference(image)
    return image


def verify_static_python(data):
    if data[:7] != b'\x7fELF\x02\x01\x01' or len(data) < 64 or struct.unpack_from('<H', data, 18)[0] != 62:
        raise ValueError('control Python must be a Linux x86-64 ELF executable')
    offset = struct.unpack_from('<Q', data, 32)[0]
    stride, count = struct.unpack_from('<HH', data, 54)
    if stride < 56 or not count or offset + count*stride > len(data):
        raise ValueError('invalid control Python ELF program headers')
    if any(struct.unpack_from('<I', data, offset + i*stride)[0] in (2, 3) for i in range(count)):
        raise ValueError('control Python must have no dynamic dependencies or interpreter')


def control_archive(source, destination):
    """Keep the static interpreter, standard library and upstream licenses."""
    import zstandard
    found = False
    with Path(source).open('rb') as raw, zstandard.ZstdDecompressor().stream_reader(raw) as stream, \
            tarfile.open(fileobj=stream, mode='r|') as archive, tarfile.open(destination, 'w') as output:
        for member in archive:
            name = member.name
            if name.startswith('python/install/'):
                relative = name.removeprefix('python/install/')
                if not (relative == 'bin/python3.13' or relative.startswith('lib/python3.13/')):
                    continue
                if any(p in ('__pycache__', 'site-packages', 'test', 'tests') or p.startswith('config-')
                       for p in Path(relative).parts):
                    continue
                name = 'python/' + relative
            elif 'LICENSE' in Path(name).name or name == 'python/PYTHON.json':
                name = 'licenses/' + Path(name).name
            else:
                continue
            info = copy.copy(member)
            info.name, info.pax_headers = name, dict(member.pax_headers)
            info.pax_headers.pop('path', None)
            content = archive.extractfile(member) if member.isreg() else None
            if name == 'python/bin/python3.13':
                data = content.read()
                verify_static_python(data)
                content, found = io.BytesIO(data), True
            output.addfile(info, content)
        if not found:
            raise ValueError('static control Python is missing from its distribution')


def metadata(config):
    values = config.get('config') or {}
    environment = {}
    for item in values.get('Env') or []:
        if not isinstance(item, str) or '=' not in item or '\0' in item:
            raise ValueError('image contains invalid environment settings')
        key, value = item.split('=', 1)
        if not key:
            raise ValueError('image environment variable has an empty name')
        environment[key] = value
    working_dir = values.get('WorkingDir') or '/'
    if not isinstance(working_dir, str) or not working_dir.startswith('/') or '\0' in working_dir:
        raise ValueError('image working directory must be an absolute path')
    user = values.get('User') or '0'
    if not isinstance(user, str) or '\0' in user:
        raise ValueError('image user must be a string')
    commands = {}
    for key in ('Entrypoint', 'Cmd'):
        command = values.get(key) or []
        if not isinstance(command, list) or any(not isinstance(v, str) or '\0' in v for v in command):
            raise ValueError('image ' + key + ' must be an argument list')
        commands[key.lower()] = command
    return {'env': environment, 'workdir': working_dir, 'user': user, **commands}


def prepare(image, root, *, refresh=False):
    """Return a pinned descriptor; a cached tag performs no registry requests."""
    from ..bootstrap import Builder, download
    from ..setup_progress import Stage
    root = Path(root)
    directory = home() / 'images'
    directory.mkdir(parents=True, exist_ok=True)
    alias = directory / 'references' / (fingerprint(reference(image)) + '.json')
    with locked(alias.with_suffix('.lock')):
        registry = Registry(image, directory / 'blobs')
        selected = json.loads(alias.read_text()) if alias.is_file() and not refresh else registry.resolve()
        settings = metadata(selected['config'])
        key = fingerprint({'digest': selected['digest'], 'python': PYTHON_SHA256, 'format': FORMAT})
        destination = directory / (key + '.erofs')
        receipt = destination.with_suffix('.json')
        with locked(directory / (key + '.lock')):
            recorded = json.loads(receipt.read_text()) if receipt.is_file() else {}
            valid = destination.is_file() and recorded.get('signature') == file_signature(destination)
            if not valid and destination.is_file() and recorded.get('sha256') == file_digest(destination):
                recorded['signature'] = file_signature(destination)
                atomic_json(receipt, recorded)
                valid = True
            if not valid:
                with Stage('Prepare ' + image, unit='layers', detail='Downloading image') as progress:
                    layers = selected['manifest']['layers']
                    progress.update(total=len(layers) + 2)
                    # The builder needs its trusted helper files in /lab. Only
                    # temporary files go here; durable image blobs use the
                    # selected Sandweave data directory.
                    with tempfile.TemporaryDirectory(prefix='.image-', dir=root) as temporary:
                        working = Path(temporary)
                        filesystem = Filesystem()
                        for number, (layer, expected) in enumerate(zip(layers, selected['config']['rootfs']['diff_ids'])):
                            archive = working / (str(number) + '.tar')
                            progress.update(detail='Layer ' + str(number + 1))
                            unpack(registry.blob(layer), archive, layer['mediaType'], expected)
                            filesystem.apply(archive)
                            progress.update(advance=1)
                        if any(name == PRIVATE or name.startswith(PRIVATE + '/') for name in filesystem.entries):
                            raise ValueError('image uses the reserved /' + PRIVATE + ' directory')
                        python = download(PYTHON_URL, directory / 'downloads', PYTHON_NAME, sha256=PYTHON_SHA256)
                        archive = working / 'control.tar'
                        control_archive(python, archive)
                        filesystem.apply(archive, prefix=PRIVATE + '/')
                        workdir = filesystem.resolve(settings['workdir'].strip('/') or '.', follow=True)
                        filesystem.parents(workdir + '/.directory')
                        progress.update(advance=1, detail='Packing filesystem')
                        merged = working / 'rootfs.tar'
                        filesystem.write(merged)
                        output = working / 'rootfs.erofs'
                        Builder(home()).erofs(root, merged, output)
                        # Worker directories may be on a different filesystem
                        # from the selected data directory.
                        staged = destination.with_suffix('.tmp')
                        _immutable(output, staged)
                        os.replace(staged, destination)
                        recorded = {'sha256': file_digest(destination), 'signature': file_signature(destination)}
                        atomic_json(receipt, recorded)
                        progress.update(advance=1, detail='Ready')
            atomic_json(alias, selected)
        relative = 'images/oci-' + key + '.erofs'
        (root / 'images').mkdir(exist_ok=True)
        _immutable(destination, root / relative)
        return {'reference': image, 'digest': selected['digest'], 'platform': selected['platform'],
                'base_image': relative, 'agent': '/' + PRIVATE + '/python/bin/python3.13',
                'settings': settings}


def configure(spec, root, *, refresh=False):
    spec = copy.deepcopy(spec)
    started = time.monotonic()
    image = prepare(spec['image']['reference'], root, refresh=refresh)
    spec['image'] = image
    recipe, settings = spec['template'], image['settings']
    spec['env'] = {**settings['env'], **spec.get('env', {})}
    if not recipe.get('_user_explicit'):
        recipe['user'] = settings['user']
    recipe.setdefault('workdir', settings['workdir'])
    return spec, time.monotonic() - started

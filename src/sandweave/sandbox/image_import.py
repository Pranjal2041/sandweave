"""Import an image produced inside a sandbox into the normal snapshot store."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import uuid
import time

from .workspace import home, locked
from .resources import Memory


def upload(worker, identity, action, *, data=b'', offset=0, sha256=None):
    """Receive one archive in this importer's private host directory."""
    record = worker.read(identity)
    if record['state'] != 'ready' or record['spec'].get('_image_import_memory', 0) < 384 * 1024**2:
        raise ValueError('archive upload requires a running image importer')
    root = worker.root / 'image-uploads' / identity
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / 'archive.tar'
    receipt = root / 'complete.json'
    if receipt.exists():
        previous = json.loads(receipt.read_text())
        if action == 'finish' and sha256 == previous['sha256'] and offset == previous['size']:
            return {'format': 'oci', 'sha256': sha256}
        raise ValueError('published image uploads are immutable')
    if action == 'write':
        if type(offset) is not int or offset < 0 or not isinstance(data, bytes) or len(data) > 4 * 1024**2:
            raise ValueError('invalid archive upload chunk')
        with path.open('r+b' if path.exists() else 'w+b') as stream:
            stream.seek(0, 2)
            if offset > stream.tell():
                raise ValueError('archive upload contains a gap')
            stream.seek(offset)
            stream.write(data)
        return {'offset': offset + len(data)}
    if action != 'finish':
        raise ValueError('unknown archive upload action')
    from ..templates.registry import digest
    digest('sha256:' + str(sha256))
    if type(offset) is not int or offset <= 0 or path.stat().st_size != offset:
        raise ValueError('archive upload size mismatch')
    from .workspace import verified_digest, _immutable
    verified = {}
    if verified_digest(path, verified=verified) != sha256:
        raise ValueError('archive upload checksum mismatch')
    destination = home() / 'images' / 'archives' / (sha256 + '.tar')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with locked(destination.with_suffix('.lock')):
        if not destination.exists():
            # Both are worker-side files, but storage roots may differ.
            _immutable(path, destination, sha256=sha256, verified=verified)
        from .retention import commit
        commit(receipt, {'sha256': sha256, 'size': offset})
        path.unlink()
    return {'format': 'oci', 'sha256': sha256}


def import_image(path, *, template=None, target=None, timeout=600):
    """Upload an OCI tar from the client and return a portable filesystem snapshot."""
    from .sandbox import Sandbox, definition
    from .snapshots import SnapshotRef
    from .resources import positive
    positive(timeout, 'timeout')
    source = Path(path)
    if not source.is_file():
        raise ValueError('image must be a regular OCI archive file')
    deadline = time.monotonic() + timeout
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError('local image import exceeded its deadline')
        return value
    request = definition(template={}, target=target, memory=Memory('256MiB', '128MiB'),
                         startup_timeout=remaining())
    request['spec']['_image_import_memory'] = 384 * 1024**2
    importer = Sandbox._from_definition(request, target)
    try:
        checksum, offset = hashlib.sha256(), 0
        with source.open('rb') as stream:
            while chunk := stream.read(4 * 1024**2):
                remaining()
                importer._call('image_upload', action='write', offset=offset, data=chunk)
                checksum.update(chunk)
                offset += len(chunk)
        archive = importer._call('image_upload', action='finish', offset=offset, sha256=checksum.hexdigest())
        from ..templates.resolve import Template
        recipe = Template(template if template is not None else {}).resolve()
        if not recipe.get('_user_explicit'):
            recipe.pop('user', None)
        saved = importer._call('image_capture', archive=archive, template=recipe, timeout=remaining())
        return SnapshotRef.from_record(saved, importer._connection.clone())
    finally:
        try:
            importer.delete(wait=False)
        finally:
            importer.close()


def volume_file(record, volume, path):
    """Open a regular file beneath a worker-owned volume, never follow guest links."""
    relative = PurePosixPath(path)
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise ValueError('image path must stay inside its volume')
    root = Path(record.get('service_volumes', {})[volume]) / 'data'
    directory = os.open(root, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for name in relative.parts[:-1]:
            child = os.open(name, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(relative.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('image archive must be a regular file')
            return os.fdopen(fd, 'rb')
        except BaseException:
            os.close(fd)
            raise
    finally:
        os.close(directory)


def capture(worker, identity, path=None, template=None, *, timeout=300, volume=None, archive=None):
    if archive is not None:
        if path is not None or volume is not None:
            raise ValueError('archive and guest image paths are alternative sources')
        from ..templates.oci_archive import validate
        validate(archive)
        return _capture(worker, identity, archive, template, timeout=timeout)
    if not isinstance(path, str) or not path:
        raise ValueError('image capture requires an archive or guest path')
    parent = worker.read(identity)
    if parent['spec'].get('_image_import_memory', 0) < 384 * 1024**2:
        raise ValueError('Image import requires a builder memory reservation')
    deadline = time.monotonic() + timeout
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError('Image import exceeded its deadline')
        return value
    agent = worker.agent(identity) if volume is None else None
    directory = home() / 'images' / 'archives'
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.import-', dir=directory)
    handle = uuid.uuid4().hex
    opened = False
    try:
        digest = hashlib.sha256()
        with os.fdopen(fd, 'wb') as output:
            if volume is not None:
                # The builder already exported here. Avoid streaming its whole
                # archive back through guest TCP, preserving the same digest.
                with volume_file(parent, volume, path) as source:
                    while data := source.read(4 * 1024**2):
                        remaining()
                        output.write(data)
                        digest.update(data)
            else:
                agent.call('file', op='open', handle=handle, path=path, mode='rb')
                opened = True
                while data := agent.call('file', op='read', handle=handle, size=1024**2):
                    remaining()
                    output.write(data)
                    digest.update(data)
        checksum = digest.hexdigest()
        destination = directory / (checksum + '.tar')
        with locked(destination.with_suffix('.lock')):
            if not destination.exists():
                os.replace(temporary, destination)
        image = {'format': 'oci', 'sha256': checksum}
        return _capture(worker, identity, image, template, timeout=remaining())
    finally:
        try:
            if opened:
                agent.call('file', op='close', handle=handle)
        finally:
            Path(temporary).unlink(missing_ok=True)


def _capture(worker, identity, image, template, *, timeout):
    from .sandbox import definition
    parent = worker.read(identity)
    if parent['spec'].get('_image_import_memory', 0) < 384 * 1024**2:
        raise ValueError('Image import requires a builder memory reservation')
    deadline = time.monotonic() + timeout
    request = definition(image=image, template=template,
        memory=Memory('256MiB', '128MiB'), startup_timeout=timeout,
        detached=parent['owner'] is None)
    child = 'sw-' + uuid.uuid4().hex
    parent.setdefault('image_imports', []).append(child)
    worker.write(parent)
    try:
        from ..templates.images import configure
        spec, image_seconds = configure(request['spec'], worker.root)
        spec['startup_timeout'] = deadline - time.monotonic()
        if spec['startup_timeout'] <= 0:
            raise TimeoutError('Image import exceeded its deadline')
        worker._create(spec, child, owner=parent['owner'], operation_id=uuid.uuid4().hex,
                       service_parent=identity)
        result = worker.capture(child, state='filesystem')
        if worker.snapshot_verify(result['id'])['status'] != 'passed':
            raise ValueError('Built image verification failed')
        return worker.snapshot_info(result['id'])
    finally:
        if worker.path(child).exists():
            worker.terminate(child)

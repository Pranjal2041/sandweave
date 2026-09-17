"""Import an image produced inside a sandbox into the normal snapshot store."""
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import uuid
import time

from .workspace import home, locked
from .resources import Memory


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


def capture(worker, identity, path, template, *, timeout=300, volume=None):
    from .sandbox import definition
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
        request = definition(image=image, template=template,
            memory=Memory('256MiB', '128MiB'), startup_timeout=timeout,
            detached=parent['owner'] is None)
        child = 'sw-' + uuid.uuid4().hex
        parent.setdefault('image_imports', []).append(child)
        worker.write(parent)
        try:
            from ..templates.images import configure
            spec, image_seconds = configure(request['spec'], worker.root)
            spec['startup_timeout'] = remaining()
            worker._create(spec, child, owner=parent['owner'], operation_id=uuid.uuid4().hex,
                           service_parent=identity)
            result = worker.capture(child, state='filesystem')
            if worker.snapshot_verify(result['id'])['status'] != 'passed':
                raise ValueError('Built image verification failed')
            return worker.snapshot_info(result['id'])
        finally:
            if worker.path(child).exists():
                worker.terminate(child)
    finally:
        try:
            if opened:
                agent.call('file', op='close', handle=handle)
        finally:
            Path(temporary).unlink(missing_ok=True)

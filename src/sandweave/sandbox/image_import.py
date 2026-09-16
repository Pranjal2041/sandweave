"""Import an image produced inside a sandbox into the normal snapshot store."""
import hashlib
import os
from pathlib import Path
import tempfile
import uuid
import time

from .workspace import home, locked
from .resources import Memory


def capture(worker, identity, path, template, *, timeout=300):
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
    agent = worker.agent(identity)
    directory = home() / 'images' / 'archives'
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.import-', dir=directory)
    handle = uuid.uuid4().hex
    opened = False
    try:
        digest = hashlib.sha256()
        with os.fdopen(fd, 'wb') as output:
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

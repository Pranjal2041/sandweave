"""Read an OCI archive without extracting archive paths onto the host."""
import hashlib
import json
import os
from pathlib import Path
import tarfile
import tempfile

from .registry import digest
from ..sandbox.workspace import home, locked


def validate(image):
    if set(image) != {'format', 'sha256'} or image.get('format') != 'oci':
        raise ValueError('OCI archive references require format and sha256')
    digest('sha256:' + image['sha256'])
    return image


class Archive:
    def __init__(self, image, directory):
        validate(image)
        self.path = home() / 'images' / 'archives' / (image['sha256'] + '.tar')
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.members = {}
        with tarfile.open(self.path, 'r:') as archive:
            for member in archive:
                name = member.name.removeprefix('./')
                if name in self.members or name.startswith('/') or '..' in Path(name).parts:
                    raise ValueError('invalid or duplicate OCI archive path')
                if not member.isdir() and not member.isfile():
                    raise ValueError('OCI archive entries must be regular files or directories')
                self.members[name] = member

    def _read(self, name, limit=16 * 1024 * 1024):
        member = self.members.get(name)
        if member is None or not member.isfile() or member.size > limit:
            raise ValueError('missing or oversized OCI metadata: ' + name)
        with self.path.open('rb') as stream:
            stream.seek(member.offset_data)
            data = stream.read(member.size)
        if len(data) != member.size:
            raise ValueError('truncated OCI archive')
        return data

    def _json(self, descriptor):
        if type(descriptor.get('size')) is not int or descriptor['size'] < 0:
            raise ValueError('invalid OCI descriptor size')
        key = digest(descriptor['digest'])
        data = self._read('blobs/sha256/' + key)
        if len(data) != descriptor['size'] or hashlib.sha256(data).hexdigest() != key:
            raise ValueError('OCI metadata digest or size mismatch')
        return json.loads(data)

    def resolve(self):
        if json.loads(self._read('oci-layout')).get('imageLayoutVersion') != '1.0.0':
            raise ValueError('unsupported OCI image layout')
        index = json.loads(self._read('index.json'))
        candidates = index.get('manifests', [])
        seen = set()
        while True:
            candidates = [entry for entry in candidates if
                not entry.get('platform') or (entry['platform'].get('os') == 'linux' and
                    entry['platform'].get('architecture') == 'amd64')]
            if len(candidates) != 1:
                raise ValueError('OCI archive must select exactly one Linux amd64 image')
            descriptor = candidates[0]
            if descriptor['digest'] in seen or len(seen) >= 16:
                raise ValueError('cyclic or excessively nested OCI image index')
            seen.add(descriptor['digest'])
            manifest = self._json(descriptor)
            if 'manifests' not in manifest:
                break
            candidates = manifest['manifests']
        if manifest.get('schemaVersion') != 2 or 'layers' not in manifest:
            raise ValueError('invalid OCI image manifest')
        config = self._json(manifest['config'])
        if config.get('os') != 'linux' or config.get('architecture') != 'amd64':
            raise ValueError('OCI archive must target linux/amd64')
        if config.get('rootfs', {}).get('type') != 'layers' or len(
                config['rootfs'].get('diff_ids', [])) != len(manifest['layers']):
            raise ValueError('OCI image layers do not match their configuration')
        for identity in config['rootfs']['diff_ids']:
            digest(identity)
        return {'digest': descriptor['digest'], 'platform': 'linux/amd64',
                'manifest': manifest, 'config': config}

    def blob(self, descriptor):
        if type(descriptor.get('size')) is not int or descriptor['size'] < 0:
            raise ValueError('invalid OCI blob size')
        key = digest(descriptor['digest'])
        member = self.members.get('blobs/sha256/' + key)
        if member is None or not member.isfile() or member.size != descriptor['size']:
            raise ValueError('missing OCI blob or size mismatch')
        destination = self.directory / key
        with locked(destination.with_suffix('.lock')):
            if destination.is_file() and destination.stat().st_size == member.size:
                with destination.open('rb') as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() == key:
                        return destination
            fd, temporary = tempfile.mkstemp(prefix='.oci-', dir=self.directory)
            try:
                checksum, remaining = hashlib.sha256(), member.size
                with os.fdopen(fd, 'wb') as output, self.path.open('rb') as source:
                    source.seek(member.offset_data)
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError('truncated OCI blob')
                        output.write(chunk)
                        checksum.update(chunk)
                        remaining -= len(chunk)
                if checksum.hexdigest() != key:
                    raise ValueError('OCI blob digest mismatch')
                os.replace(temporary, destination)
            finally:
                Path(temporary).unlink(missing_ok=True)
        return destination

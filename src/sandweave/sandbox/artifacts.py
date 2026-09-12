"""Verified snapshot transport, confined to a private import directory."""
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from .errors import IncompatibleSnapshot
from .wire import encode, decode
from .workspace import locked, atomic_json, _immutable


def cache_path(value):
    """Keep worker paths literal; never resolve them against the client's cwd."""
    if value is None:
        return None
    value = os.fspath(value)
    if not isinstance(value, str) or '\0' in value or not Path(value).is_absolute() or '..' in Path(value).parts:
        raise ValueError('shared_cache must be an absolute path on the workers')
    return value


class Artifacts:
    def __init__(self, worker, *, shared_cache=None):
        self.worker = worker
        self.exports = {}
        self.root = (Path(cache_path(shared_cache)) / 'artifacts-v1' if shared_cache is not None
                     else worker.store.root / 'imports')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def cached(self, reference):
        directory = self.directory(reference)
        # Paths and hostnames cannot tell whether two workers see the same
        # storage. A marker created under a filesystem lock can.
        with locked(self.root / '.identity.lock'):
            marker = self.root / 'cache.json'
            if not marker.exists():
                atomic_json(marker, {'id': uuid.uuid4().hex})
            identity = json.loads(marker.read_text())['id']
        with locked(directory.with_suffix('.lock')):
            ready = (directory / 'complete').exists()
            if ready:
                self._attach(reference)
        return {'cache_id': identity, 'ready': ready, 'id': reference}

    def cache(self, reference):
        """Publish locally visible immutable files once into the selected cache."""
        with locked(self.directory(reference).with_suffix('.lock')):
            if not (self.directory(reference) / 'complete').exists():
                manifest = self.manifest(reference)
                for name in self._begin(manifest):
                    _immutable(self.exports[reference][name], self.directory(reference) / name,
                               sha256=manifest['files'][name]['sha256'])
                result = self._finish(reference)
                atomic_json(self.directory(reference) / 'complete', result)
            else:
                self._attach(reference)
        return self.cached(reference)

    def directory(self, reference):
        import re
        if not re.fullmatch(r'snap-[0-9a-f]{32}', reference):
            raise ValueError('artifact import requires an immutable snapshot ID')
        return self.root / reference

    @staticmethod
    def relative(value):
        path = Path(value)
        if path.is_absolute() or '..' in path.parts or not path.parts or '\0' in value:
            raise ValueError('artifact path escapes its workspace')
        return path

    def metadata(self, reference):
        saved = self.worker.store.resolve(reference)
        if self.worker.store.verify(saved)['status'] != 'passed':
            raise IncompatibleSnapshot('snapshot verification failed')
        return saved

    def _publish(self, metadata):
        path = self.worker.store.root / 'revisions' / (metadata['id'] + '.bin')
        with locked(path.with_suffix('.lock')):
            if path.exists():
                previous = decode(path.read_bytes())
                if previous['digest'] != metadata['digest']:
                    raise IncompatibleSnapshot('snapshot ID has a different digest')
            temporary = path.with_suffix('.importing')
            with temporary.open('wb') as stream:
                stream.write(encode(metadata)); stream.flush(); os.fsync(stream.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, path)
        return {'id': metadata['id']}

    def import_shared(self, metadata):
        self.directory(metadata['id'])
        if not Path(metadata['location']).is_dir() or not Path(metadata['workspace']).is_dir():
            raise FileNotFoundError('snapshot is not on shared storage')
        if self.worker.store.verify(metadata)['status'] != 'passed':
            raise IncompatibleSnapshot('shared snapshot verification failed')
        return self._publish(metadata)

    def manifest(self, reference):
        saved = self.metadata(reference)
        snapshot, workspace = Path(saved['location']), Path(saved['workspace'])
        manifest = json.loads((snapshot / 'snapshot-manifest.json').read_text())
        roots = [('snapshot', snapshot)]
        base = self.relative(manifest['base_image']['path'])
        roots.append(('workspace/' + str(base), workspace / base))
        if saved['spec']['runtime'] != 'apptainer':
            runtime = self.relative(manifest['runtime']['path'])
            roots.append(('workspace/' + str(runtime), workspace / runtime))
        files, paths = {}, {}
        for prefix, root in roots:
            for path in [root, *sorted(root.rglob('*'))] if root.is_dir() else [root]:
                if prefix == 'snapshot' and path.parent == root and path.name in (
                        'verification.json', 'verification.log', '.verification.lock'):
                    continue  # Verification rewrites these; they are not frozen content.
                name = prefix if path == root else prefix + '/' + str(path.relative_to(root))
                info = path.lstat()
                entry = {'mode': stat.S_IMODE(info.st_mode), 'xattrs': {
                    k: os.getxattr(path, k, follow_symlinks=False).hex() for k in os.listxattr(path, follow_symlinks=False)}}
                if path.is_symlink():
                    entry.update(kind='symlink', target=os.readlink(path))
                elif path.is_dir():
                    entry.update(kind='directory')
                elif path.is_file():
                    with path.open('rb') as stream:
                        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
                    entry.update(kind='file', size=info.st_size, sha256=checksum)
                else:
                    raise IncompatibleSnapshot('unsupported artifact entry: ' + name)
                files[name], paths[name] = entry, path
        self.exports[saved['id']] = paths
        return {'metadata': saved, 'files': files}

    def read(self, reference, path, offset, size):
        self.relative(path)
        if reference not in self.exports:
            self.manifest(reference)
        source = self.exports[reference][path]
        if source.is_symlink() or not source.is_file():
            raise ValueError('artifact entry is not a regular file')
        if type(offset) is not int or offset < 0 or type(size) is not int or not 0 < size <= 1024**2:
            raise ValueError('invalid artifact read range')
        with source.open('rb') as stream:
            stream.seek(offset)
            return stream.read(size)

    def begin(self, manifest):
        with locked(self.directory(manifest['metadata']['id']).with_suffix('.lock')):
            return self._begin(manifest)

    def _begin(self, manifest):
        directory = self.directory(manifest['metadata']['id'])
        directory.mkdir(exist_ok=True, mode=0o700)
        for name, info in manifest['files'].items():
            relative = self.relative(name)
            if relative.parts[0] not in ('workspace', 'snapshot'):
                raise ValueError('unknown artifact root')
            if info['kind'] not in ('file', 'symlink', 'directory'):
                raise ValueError('unknown artifact entry type')
            # Never place a child through a symlink, even a dangling one.
            for parent in relative.parents:
                if str(parent) in manifest['files'] and manifest['files'][str(parent)]['kind'] != 'directory':
                    raise ValueError('artifact has a non-directory parent')
        descriptor = directory / 'manifest.bin'
        if descriptor.exists() and self.content(decode(descriptor.read_bytes())) != self.content(manifest):
            raise IncompatibleSnapshot('artifact import already has a different manifest')
        if (directory / 'complete').exists():
            return []
        if not descriptor.exists():
            temporary = descriptor.with_suffix('.importing')
            with temporary.open('wb') as stream:
                stream.write(encode(manifest)); stream.flush(); os.fsync(stream.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, descriptor)
        missing = []
        for name, info in sorted(manifest['files'].items(), key=lambda p: len(Path(p[0]).parts)):
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if info['kind'] == 'directory':
                path.mkdir(exist_ok=True)
            elif info['kind'] == 'symlink':
                if not path.is_symlink():
                    path.symlink_to(info['target'])
                elif os.readlink(path) != info['target']:
                    raise IncompatibleSnapshot('artifact symlink differs')
            else:
                if name.startswith('workspace/'):
                    existing = self.worker.root / name.removeprefix('workspace/')
                    if (existing.is_file() and existing.stat().st_size == info['size'] and
                            stat.S_IMODE(existing.stat().st_mode) == info['mode'] and
                            {k: os.getxattr(existing, k).hex() for k in os.listxattr(existing)} == info.get('xattrs', {})):
                        try:
                            _immutable(existing, path, sha256=info['sha256'])
                            continue
                        except Exception:
                            pass
                if path.is_symlink():
                    raise ValueError('artifact file cannot be a symlink')
                if not path.exists():
                    path.touch(mode=0o600)
                missing.append(name)
        return missing

    def write(self, reference, path, offset, data):
        with locked(self.directory(reference).with_suffix('.lock')):
            return self._write(reference, path, offset, data)

    @staticmethod
    def content(manifest):
        # Replication rewrites only these host locations. The snapshot digest,
        # recipe and complete file manifest must still match across sources.
        metadata = {k: v for k, v in manifest['metadata'].items() if k not in ('workspace', 'location')}
        return {**manifest, 'metadata': metadata}

    def _write(self, reference, path, offset, data):
        directory = self.directory(reference)
        relative = self.relative(path)
        manifest = decode((directory / 'manifest.bin').read_bytes())
        info = manifest['files'][str(relative)]
        if info['kind'] != 'file' or type(offset) is not int or offset < 0 or len(data) > 1024**2 or offset + len(data) > info['size']:
            raise ValueError('invalid artifact write range')
        destination = directory / relative
        if any(p.is_symlink() for p in [destination, *destination.parents] if p != directory.parent):
            raise ValueError('artifact write cannot follow a symlink')
        if (directory / 'complete').exists():
            # Another importer can publish while this caller's identical chunk
            # is in flight. Acknowledge a matching retry without writing bytes.
            with destination.open('rb') as stream:
                stream.seek(offset)
                if stream.read(len(data)) != data:
                    raise IncompatibleSnapshot('published artifacts are immutable')
            return len(data)
        with destination.open('r+b') as stream:
            stream.seek(offset)
            stream.write(data)
        return len(data)

    def finish(self, reference):
        with locked(self.directory(reference).with_suffix('.lock')):
            complete = self.directory(reference) / 'complete'
            if complete.exists():
                # The bytes may have been published by another worker. Each
                # worker still needs its own private revision record.
                return self._attach(reference)
            result = self._finish(reference)
            atomic_json(self.directory(reference) / 'complete', result)
            return result

    def _finish(self, reference):
        directory = self.directory(reference)
        manifest = decode((directory / 'manifest.bin').read_bytes())
        for name, info in manifest['files'].items():
            path = directory / self.relative(name)
            if info['kind'] == 'file':
                with path.open('rb') as stream:
                    if path.stat().st_size != info['size'] or hashlib.file_digest(stream, 'sha256').hexdigest() != info['sha256']:
                        raise IncompatibleSnapshot('artifact checksum mismatch: ' + name)
                    os.fsync(stream.fileno())
            if info['kind'] != 'symlink' and stat.S_IMODE(path.stat().st_mode) != info['mode']:
                path.chmod(info['mode'])
            for key, value in info.get('xattrs', {}).items():
                if key not in os.listxattr(path, follow_symlinks=False) or os.getxattr(path, key, follow_symlinks=False).hex() != value:
                    os.setxattr(path, key, bytes.fromhex(value), follow_symlinks=False)
        saved = self._saved(reference)
        if self.worker.store.verify(saved)['status'] != 'passed':
            raise IncompatibleSnapshot('imported snapshot failed verification')
        return self._publish(saved)

    def _saved(self, reference):
        directory = self.directory(reference)
        saved = copy.deepcopy(decode((directory / 'manifest.bin').read_bytes())['metadata'])
        saved.update(workspace=str(directory / 'workspace'), location=str(directory / 'snapshot'))
        return saved

    def _attach(self, reference):
        saved = self._saved(reference)
        record = self.worker.store.root / 'revisions' / (reference + '.bin')
        # The normal snapshot-info fast path also trusts an already verified
        # immutable revision. Do not hash gigabytes again for every lease.
        if record.exists() and decode(record.read_bytes()) == saved:
            return {'id': reference}
        return self.import_shared(saved)

    def dispatch(self, operation, parameters):
        from . import retention
        if operation == 'artifact_release':
            return retention.release(self.worker, **parameters)
        parameters = dict(parameters)
        shared_cache = parameters.pop('shared_cache', None)
        if shared_cache is not None:
            if operation not in ('artifact_cached', 'artifact_cache', 'artifact_begin', 'artifact_write', 'artifact_finish'):
                raise ValueError('shared_cache is not supported for this artifact operation')
            return Artifacts(self.worker, shared_cache=shared_cache).dispatch(operation, parameters)
        operation = operation.removeprefix('artifact_')
        method = {'metadata': self.metadata, 'import': self.import_shared, 'manifest': self.manifest,
                  'read': self.read, 'begin': self.begin, 'write': self.write, 'finish': self.finish,
                  'cached': self.cached, 'cache': self.cache}.get(operation)
        if method is None:
            raise ValueError('unknown artifact operation')
        metadata = parameters.get('metadata') or parameters.get('manifest', {}).get('metadata')
        if metadata is None and parameters.get('reference'):
            reference = parameters['reference']
            descriptor = self.directory(reference) / 'manifest.bin'
            if descriptor.exists():
                metadata = decode(descriptor.read_bytes())['metadata']
            else:
                from .errors import CacheMiss
                try:
                    metadata = self.worker.store.resolve(reference)
                except CacheMiss:
                    pass
        pool = (metadata or {}).get('spec', {}).get('_retention_pool')
        with retention.guard(pool):
            retention.active(pool)
            return method(**parameters)

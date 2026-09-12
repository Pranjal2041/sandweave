"""Immutable saved environments and atomic names in the configured artifact store."""
import copy
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time

from .asyncio import dualmethod
from .errors import CacheConflict, CacheMiss, IncompatibleSnapshot
from .wire import encode, decode
from .workspace import home, locked, atomic_json, _immutable, file_signature
from . import retention


@dataclass(frozen=True)
class SnapshotRef:
    id: str
    state: str
    location: str
    digest: str
    source: str
    template: str
    _connection: object = field(repr=False, compare=False)

    @classmethod
    def from_record(cls, record, connection):
        return cls(**{k: record[k] for k in ('id', 'state', 'location', 'digest', 'source', 'template')},
                   _connection=connection)

    @property
    def verification(self):
        return self._connection.call('snapshot_info', reference=self.id)['verification']

    @property
    def dependencies(self):
        return self._connection.call('snapshot_info', reference=self.id)['dependencies']

    @dualmethod
    def verify(self):
        return self._connection.call('snapshot_verify', reference=self.id)

    def __str__(self):
        return self.id


class Store:
    def __init__(self, runtime):
        self.runtime = runtime
        self.materialized = {}
        self.root = home() / 'store'
        for directory in ('revisions', 'names', 'locks'):
            (self.root / directory).mkdir(parents=True, exist_ok=True, mode=0o700)

    def name_path(self, name):
        if not isinstance(name, str) or not name or len(name) > 256 or '\0' in name:
            raise ValueError('cache name must be a nonempty string of at most 256 characters')
        return self.root / 'names' / (hashlib.sha256(name.encode()).hexdigest() + '.json')

    def name_lock(self, name):
        return locked(self.root / 'locks' / self.name_path(name).stem)

    def alias(self, name):
        path = self.name_path(name)
        return json.loads(path.read_text()) if path.exists() else None

    def publish(self, name, revision, expected, *, provenance='captured', fingerprint=None):
        # Caller reads the expected revision before starting its capture/build.
        pool = self.resolve(revision['id']).get('spec', {}).get('_retention_pool')
        with retention.guard(pool), self.name_lock(name):
            retention.active(pool)
            current = self.alias(name)
            if (current or {}).get('id') != expected:
                raise CacheConflict('cache name changed during publication: ' + name)
            if current and current['provenance'] != provenance:
                raise CacheConflict('cache name has incompatible provenance: ' + name)
            atomic_json(self.name_path(name), {'name': name, 'id': revision['id'],
                        'provenance': provenance, 'fingerprint': fingerprint})

    def resolve(self, reference):
        if not re.fullmatch(r'snap-[0-9a-f]{32}', str(reference)):
            alias = self.alias(str(reference))
            if alias is None:
                raise CacheMiss('cache does not exist: ' + str(reference))
            reference = alias['id']
        path = self.root / 'revisions' / (str(reference) + '.bin')
        if not path.is_file():
            raise CacheMiss('saved revision does not exist: ' + str(reference))
        record = decode(path.read_bytes())
        retention.active(record.get('spec', {}).get('_retention_pool'))
        # Metadata includes preparation inputs and the live agent secret. It is
        # private to the worker; only public() crosses the control boundary.
        return record

    def record(self, identity, saved, source):
        pool = source['spec'].get('_retention_pool')
        with retention.guard(pool):
            retention.active(pool)
            return self._record(identity, saved, source)

    def _record(self, identity, saved, source):
        path = Path(saved['snapshot'])
        manifest = json.loads((path / 'snapshot-manifest.json').read_text())
        metadata = {'id': identity, 'state': 'filesystem' if saved['kind'] == 'filesystem' else 'memory',
                    'location': str(path), 'workspace': str(self.runtime.root), 'source': source['id'],
                    'template': source['spec']['template']['name'], 'spec': copy.deepcopy(source['spec']),
                    'agent': source['agent'], 'created_at': time.time(), 'snapshot_id': manifest['snapshot_id']}
        metadata['digest'] = hashlib.sha256(encode(metadata)).hexdigest()
        destination = self.root / 'revisions' / (identity + '.bin')
        fd, temporary = tempfile.mkstemp(prefix='.' + identity, dir=destination.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(encode(metadata)); stream.flush(); os.fsync(stream.fileno())
            # IDs are randomly generated; never silently replace a revision.
            os.link(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return metadata

    def public(self, record):
        path = Path(record['location'])
        manifest = json.loads((path / 'snapshot-manifest.json').read_text())
        status = path / 'verification.json'
        return {**{k: record[k] for k in ('id', 'state', 'location', 'digest', 'source', 'template')},
                'dependencies': {**{k: manifest[k] for k in ('runtime', 'base_image', 'files')},
                                 'external_mounts': record['spec'].get('mounts', [])},
                'verification': json.loads(status.read_text()) if status.exists() else {'status': 'missing'}}

    def verify(self, record, *, reuse=False, verified=None):
        if record.get('spec', {}).get('runtime', 'gvisor') == 'apptainer':
            from .runtimes.apptainer.driver import verify
            return verify(Path(record['workspace']), Path(record['location']))
        import snapshot_store
        return snapshot_store.verify(Path(record['workspace']), Path(record['location']),
                                     verified=verified, reuse=reuse)

    def materialize(self, record):
        """Make all engine-relative inputs available on this worker, without aliases outside it."""
        pool = record.get('spec', {}).get('_retention_pool')
        with retention.guard(pool):
            retention.active(pool)
            return self._materialize(record)

    def _materialize(self, record):
        import snapshot_store
        source, workspace = Path(record['location']), Path(record['workspace'])
        native = record.get('spec', {}).get('runtime', 'gvisor') == 'apptainer'
        manifest = (json.loads((source / 'snapshot-manifest.json').read_text()) if native
                    else snapshot_store.inspect(workspace, source))
        if native and self.verify(record)['status'] != 'passed':
            raise IncompatibleSnapshot('native snapshot integrity verification failed')
        if workspace == self.runtime.root:
            return source
        # Initial integrity completion needs its source node. Never import a
        # partially hashed checkpoint and later guess whether its inputs match.
        verification = json.loads((source / 'verification.json').read_text())
        if verification['status'] != 'passed':
            if self.verify(record)['status'] != 'passed':
                raise IncompatibleSnapshot('snapshot integrity verification failed')
            manifest = snapshot_store.inspect(workspace, source)
            verification = json.loads((source / 'verification.json').read_text())
        verified = verification.get('verified_files', {})
        destination = self.runtime.root / 'snapshots' / record['id']
        with locked(destination.parent / ('.' + record['id'] + '.import.lock')):
            dependencies = [manifest['base_image']] if native else [manifest['base_image'], manifest['runtime']]
            def signatures():
                paths = [destination / name for name in [*manifest['files'], 'snapshot-manifest.json']]
                for info in dependencies:
                    relative = Path(info['path'])
                    if relative.is_absolute() or '..' in relative.parts:
                        raise IncompatibleSnapshot('snapshot dependency escapes its workspace')
                    for root in (workspace / relative, self.runtime.root / relative):
                        paths.extend(sorted(root.rglob('*')) if root.is_dir() else [root])
                return {str(p): file_signature(p) for p in paths if not p.is_dir()}
            try:
                # Immutable copies already checked by this worker need only
                # file-identity checks. Rehashing a shared image on every lease
                # would read its entire payload over NFS again each time.
                if self.materialized.get(record['id']) == signatures():
                    return destination
            except FileNotFoundError:
                pass
            for info in dependencies:
                relative = Path(info['path'])
                if relative.is_absolute() or '..' in relative.parts:
                    raise IncompatibleSnapshot('snapshot dependency escapes its workspace')
                src, dst = workspace / relative, self.runtime.root / relative
                if src.is_dir():
                    def copy_file(source_path, destination_path):
                        checksum = info.get('sha256', {}).get(str(Path(source_path).relative_to(src)))
                        _immutable(source_path, destination_path, sha256=checksum, verified=verified)
                    shutil.copytree(src, dst, copy_function=copy_file, dirs_exist_ok=True)
                else:
                    _immutable(src, dst, sha256=info.get('sha256'), verified=verified)
            if not destination.exists():
                temporary = destination.with_name('.' + destination.name + '.importing')
                if temporary.exists():
                    shutil.rmtree(temporary)
                shutil.copytree(source, temporary, symlinks=True)
                temporary.rename(destination)
        if native:
            from .runtimes.apptainer.driver import verify
            if verify(self.runtime.root, destination)['status'] != 'passed':
                raise IncompatibleSnapshot('imported native snapshot integrity verification failed')
        else:
            snapshot_store.inspect(self.runtime.root, destination)
        with locked(destination.parent / ('.' + record['id'] + '.import.lock')):
            self.materialized[record['id']] = signatures()
        return destination

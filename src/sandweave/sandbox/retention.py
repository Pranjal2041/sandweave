"""Reclaim a pool's private files after its last sandbox and saved name release them."""
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

from .errors import CacheMiss, ResourceUnavailable
from .wire import decode
from .workspace import atomic_json, home, locked


def validate(pool):
    if not isinstance(pool, str) or not re.fullmatch(r'pool-[0-9a-f]{32}', pool):
        raise ValueError('invalid retention pool ID')
    return pool


def directory(pool):
    return home() / 'store' / 'pools' / validate(pool)


def guard(pool):
    return locked(home() / 'store' / 'locks' / validate(pool)) if pool else nullcontext()


def marker(pool):
    return directory(pool).with_suffix('.json')


def active(pool):
    if pool and marker(pool).exists():
        raise CacheMiss('pool snapshot is being released: ' + pool)


def commit(path, value):
    atomic_json(path, value)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def pin(pool, workspace, identity):
    if not pool:
        return
    with guard(pool):
        active(pool)
        key = hashlib.sha256((str(workspace) + '\0' + identity).encode()).hexdigest()
        commit(directory(pool) / 'pins' / (key + '.json'),
               {'workspace': str(workspace), 'sandbox': identity})


def unpin(pool, workspace, identity):
    if pool:
        with guard(pool):
            key = hashlib.sha256((str(workspace) + '\0' + identity).encode()).hexdigest()
            (directory(pool) / 'pins' / (key + '.json')).unlink(missing_ok=True)


def shared_path(path, pool):
    return str(Path(path) / 'pools' / validate(pool)) if path is not None else None


def capture(pool, identity, source):
    if pool:
        with guard(pool):
            active(pool)
            commit(directory(pool) / 'captures' / (identity + '.json'), {'id': identity, 'source': source})


def remove(path):
    # Never follow a replacement symlink into an unrelated directory.
    if path.is_symlink():
        raise ValueError('refusing to reclaim a symlink: ' + str(path))
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass


def release(worker, pool, sources, references=(), shared_cache=None):
    """Idempotent worker RPC. Keep tombstones and journals; delete only owned payloads."""
    from .artifacts import cache_path
    shared_cache = cache_path(shared_cache)
    validate(pool)
    sources = set(sources)
    for identity in sources:
        worker.path(identity)
    with guard(pool):
        pins = directory(pool) / 'pins'
        if any(pins.glob('*.json')):
            raise ResourceUnavailable('pool files are still used by a sandbox')
        previous = json.loads(marker(pool).read_text()) if marker(pool).exists() else {}
        records = []
        for path in (worker.store.root / 'revisions').glob('*.bin'):
            record = decode(path.read_bytes())
            if record.get('spec', {}).get('_retention_pool') == pool:
                if record['source'] not in sources:
                    raise ResourceUnavailable('pool files are retained by another saved snapshot')
                records.append(record)
        captures = [json.loads(p.read_text()) for p in (directory(pool) / 'captures').glob('*.json')]
        if any(c['source'] not in sources for c in captures):
            raise ResourceUnavailable('pool files are retained by another snapshot capture')
        references = (set(references) | set(previous.get('references', ())) |
                      {r['id'] for r in records} | {c['id'] for c in captures})
        for reference in references:
            if not re.fullmatch(r'snap-[0-9a-f]{32}', reference):
                raise ValueError('invalid snapshot ID')
            path = worker.store.root / 'revisions' / (reference + '.bin')
            if path.exists() and decode(path.read_bytes()).get('spec', {}).get('_retention_pool') != pool:
                raise ResourceUnavailable('snapshot does not belong to this retention pool')
        if any(json.loads(p.read_text()).get('id') in references
               for p in (worker.store.root / 'names').glob('*.json')):
            raise ResourceUnavailable('pool snapshot is retained by a cache name')
        # Commit the plan before deleting anything. Other worker workspaces
        # sharing this store and retries after partial deletion reuse it.
        commit(marker(pool), {'references': sorted(references), 'state': 'retiring'})
        for reference in references:
            snapshot = worker.root / 'snapshots' / reference
            if snapshot.exists():
                with locked(snapshot / '.verification.lock'):
                    remove(snapshot)
            for suffix in ('.publishing', '.importing'):
                remove(snapshot.with_name('.' + reference + suffix))
            local = worker.runtime.adapter('gvisor').manager.local
            remove(local / 'gvisor/checkpoints' / reference)
            remove(worker.store.root / 'imports' / reference)
            (worker.store.root / 'revisions' / (reference + '.bin')).unlink(missing_ok=True)
            worker.store.materialized.pop(reference, None)
            worker.artifacts.exports.pop(reference, None)
        # Image downloads and worker copies have a separate pool namespace.
        # Shared installation files and other pools' images are never candidates.
        remove(home() / 'images' / 'pools' / pool)
        remove(worker.root / 'images' / 'pools' / pool)
        remove(worker.root / 'pool-builds' / pool)
        if shared_cache is not None:
            remove(Path(shared_path(shared_cache, pool)))
        remove(directory(pool))
        commit(marker(pool), {'references': sorted(references), 'state': 'released'})
        return {'released': True, 'references': sorted(references)}

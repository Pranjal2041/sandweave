"""Move immutable baselines through authenticated worker connections."""
import hashlib

from .artifacts import cache_path
from .wire import encode
from .workspace import home, locked


def transfer(source, destination, reference, *, shared_cache=None, lock_root=None):
    shared_cache = cache_path(shared_cache)
    if shared_cache is None:
        metadata = source.call('artifact_metadata', reference=reference)
        try:
            destination.call('artifact_import', metadata=metadata)
            return reference
        except FileNotFoundError:
            _copy(source, destination, reference)
        return reference

    options = {'reference': reference, 'shared_cache': shared_cache}
    cached = destination.call('artifact_cached', **options)
    if cached['ready']:
        return reference
    # A controller serializes transfers to the same actual cache, including
    # workers using different mount paths. RPC retries remain byte-idempotent
    # if separate controllers happen to populate that cache concurrently.
    root = lock_root if lock_root is not None else home() / 'store' / 'transfers'
    key = hashlib.sha256(encode([cached['cache_id'], reference])).hexdigest()
    with locked(root / (key + '.lock')):
        if destination.call('artifact_cached', **options)['ready']:
            return reference
        # On shared storage this is a local link/copy, once under a filesystem
        # lock. The following probe then needs no payload transfer over RPC.
        source.call('artifact_cache', **options)
        if not destination.call('artifact_cached', **options)['ready']:
            _copy(source, destination, reference, shared_cache=shared_cache)
    return reference


def _copy(source, destination, reference, *, shared_cache=None):
    options = {'shared_cache': shared_cache} if shared_cache is not None else {}
    manifest = source.call('artifact_manifest', reference=reference)
    missing = destination.call('artifact_begin', manifest=manifest, **options)
    for path in missing:
        size = manifest['files'][path]['size']
        for offset in range(0, size, 1024**2):
            length = min(1024**2, size - offset)
            data = source.call('artifact_read', reference=reference, path=path, offset=offset, size=length)
            if len(data) != length:
                raise OSError('artifact source returned an incomplete file: ' + path)
            destination.call('artifact_write', reference=reference, path=path, offset=offset, data=data, **options)
    destination.call('artifact_finish', reference=reference, **options)

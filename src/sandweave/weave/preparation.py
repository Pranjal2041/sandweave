"""One image transfer per destination store; launches resume from its future."""
from concurrent.futures import ThreadPoolExecutor
import threading

from .connections import worker_key
from ..sandbox.errors import UnsupportedFeature


class Preparation:
    def __init__(self, controller, workers):
        self.controller = controller
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='weave-image')
        self.lock = threading.Lock()
        self.transfers, self.waiters = {}, {}

    def request(self, record, connection, endpoint, cache):
        reference = record['request']['reference']
        # Resolve actual storage identity, not a mount path or hostname. This
        # probe never takes the artifact's publication lock.
        try:
            destination = (('cache', connection.call('artifact_cache_identity', shared_cache=cache))
                           if cache is not None else ('worker', worker_key(endpoint)))
        except ValueError as error:
            if str(error) == 'unknown artifact operation':
                raise UnsupportedFeature('shared image preparation requires Sandweave 0.2.15 or newer on the worker') from error
            raise
        key = (destination, reference)
        with self.lock:
            future = self.transfers.get(key)
            if future is None or future.done():
                future = self.executor.submit(self._populate, reference, connection, endpoint, cache)
                self.transfers[key] = future
            self.waiters[record['id']] = (record['generation'], future, cache)

    def _populate(self, reference, connection, endpoint, cache):
        from .artifacts import ensure
        reference = ensure(self.controller, reference, connection, endpoint, cache) or reference
        if cache is not None and not connection.call('artifact_cached', reference=reference, shared_cache=cache)['ready']:
            # A local revision can satisfy ensure before the shared copy exists.
            # Publish it before resuming other workers waiting on this cache.
            connection.call('artifact_cache', reference=reference, shared_cache=cache)
        return reference

    def waiting(self, record):
        with self.lock:
            previous = self.waiters.get(record['id'])
            return bool(previous and previous[0] == record['generation'] and not previous[1].done())

    def take(self, record):
        with self.lock:
            return self.waiters.pop(record['id'], None)

    def reap(self, active):
        with self.lock:
            self.waiters = {k: v for k, v in self.waiters.items() if active.get(k) == v[0]}
            self.transfers = {k: v for k, v in self.transfers.items() if not v.done()}

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)

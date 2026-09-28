"""Worker-owned deletion intents survive client exits and worker restarts."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

from .errors import UnsupportedFeature
from .retention import commit, remove


class Deletions:
    def __init__(self, worker):
        self.worker = worker
        self.root = worker.root / 'deletions'
        self.root.mkdir(exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.pending = {}
        for path in self.root.glob('*.json'):
            request = json.loads(path.read_text())
            if request['state'] != 'deleted':
                self.pending[path.stem] = request
        self.running = {}
        self.executor = ThreadPoolExecutor(4, thread_name_prefix='sandweave-delete')
        self.closed = False

    def path(self, identity):
        self.worker.path(identity)
        return self.root / (identity + '.json')

    def status(self, identity):
        path = self.path(identity)
        return json.loads(path.read_text()) if path.exists() else {'id': identity, 'state': 'not_requested'}

    def request(self, identity):
        # Caller holds the allocation lock. Persist before acknowledging or
        # starting work; a lost acknowledgement can safely be retried.
        record = self.worker.read(identity)
        if not record.get('deleted') and not hasattr(self.worker.runtime.adapter(record['spec']['runtime']), 'delete'):
            raise UnsupportedFeature('this runtime does not support sandbox deletion')
        with self.lock:
            previous = self.status(identity)
            if previous['state'] == 'not_requested':
                previous = {'id': identity, 'state': 'pending', 'requested_at': time.time()}
                commit(self.path(identity), previous)
                self.pending[identity] = previous
        self.tick()
        return previous

    def tick(self):
        with self.lock:
            if self.closed:
                return
            self.running = {k: f for k, f in self.running.items() if not f.done()}
            for identity, request in self.pending.items():
                if len(self.running) >= 4:
                    break
                if identity not in self.running and time.time() >= request.get('retry_at', 0):
                    self.running[identity] = self.executor.submit(self._run, identity)

    def _run(self, identity):
        try:
            self._purge(identity)
            with self.lock:
                result = {**self.pending[identity], 'state': 'deleted', 'finished_at': time.time()}
                result.pop('error', None)
                result.pop('retry_at', None)
                commit(self.path(identity), result)
                self.pending.pop(identity)
        except Exception as error:
            with self.lock:
                result = {**self.pending[identity], 'error': str(error), 'retry_at': time.time() + 1}
                commit(self.path(identity), result)
                self.pending[identity] = result

    def _purge(self, identity):
        with self.worker.lock(identity):
            record = self.worker.read(identity)
            if record.get('deleted'):
                return
            self.worker.terminate(identity)
            children = set(record.get('image_imports', ()))
            children.update(item['identity'] for item in record.get('services', {}).values())
            for child in children:
                if self.worker.path(child).exists():
                    self._purge(child)
            self.worker.runtime.adapter(record['spec']['runtime']).delete(identity)
            if record['spec'].get('recording'):
                self.worker.recordings.dispatch(identity, 'delete')
            remove(self.worker.root / 'image-uploads' / identity)
            # Retain only a tombstone: delayed creates cannot reuse this ID.
            # Published snapshots, shared images and external mounts are owned
            # separately and are never reclaimed by sandbox deletion.
            tombstone = {k: record[k] for k in ('id', 'name', 'operation_id', 'created_at', 'workspace') if k in record}
            tombstone.update(state='terminated', deleted=True, cleanup_complete=True,
                owner=None, capabilities={}, timings={}, spec={
                    'runtime': record['spec']['runtime'],
                    'resources': record['spec'].get('resources', {}),
                    'template': {'name': record['spec'].get('template', {}).get('name'), 'capabilities': {}},
                    'detached': True})
            self.worker.write(tombstone, durable=True)
            self.worker.controls.pop(identity, None)

    def close(self):
        with self.lock:
            self.closed = True
        self.executor.shutdown(wait=True)

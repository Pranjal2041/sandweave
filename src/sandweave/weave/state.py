"""Indexed memory records with ordered background persistence.

Readers never acquire the transaction or database lock. Critical transactions
publish only after a durable commit. Recoverable observations publish immediately
and are flushed by the same ordered writer. Returned values belong to the caller.
"""
from collections import defaultdict, deque
from concurrent.futures import Future
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import sqlite3
import threading
import time

from ..sandbox.wire import encode, decode


def clone(value, *, metadata=False):
    if isinstance(value, dict):
        return {k: clone(v, metadata=metadata) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clone(v, metadata=metadata) for v in value]
    if isinstance(value, (bytearray, memoryview)):
        return None if metadata else bytes(value)
    if metadata and isinstance(value, bytes):
        return None
    return value


def project(record, fields):
    if fields is None:
        return record
    values = {}
    for key in fields:
        value = record
        for component in key.split('.'):
            value = value.get(component) if isinstance(value, dict) else None
        values[key] = value
    return values


class State:
    FIELDS = ('state', 'parent', 'worker', 'owner', 'released')

    def __init__(self, directory):
        self.root = Path(directory).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.authority = (self.root / 'controller.lock').open('a')
        try:
            fcntl.flock(self.authority, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.authority.close()
            raise RuntimeError('a controller already owns ' + str(self.root)) from None
        self.lock, self.cache_lock = threading.RLock(), threading.RLock()
        self.local = threading.local()
        self.records = defaultdict(dict)
        self.index = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))
        self.condition = threading.Condition()
        self.pending = deque()
        self.error = None
        self.closed = False
        self.db = sqlite3.connect(self.root / 'state.sqlite', isolation_level=None,
                                  check_same_thread=False, timeout=30)
        os.chmod(self.root / 'state.sqlite', 0o600)
        # SQLite is the durability journal, not the request-serving database.
        # Rollback mode preserves existing single-controller NFS installations.
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            self.authority.close()
            raise RuntimeError('unsupported Weave database version: ' + str(version))
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS records (
                kind TEXT NOT NULL, id TEXT NOT NULL, state TEXT, parent TEXT,
                worker TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                version INTEGER NOT NULL, data BLOB NOT NULL,
                PRIMARY KEY (kind, id));
            CREATE INDEX IF NOT EXISTS records_state ON records(kind, state);
            CREATE INDEX IF NOT EXISTS records_parent ON records(kind, parent);
            CREATE INDEX IF NOT EXISTS records_worker ON records(kind, worker);
            CREATE TABLE IF NOT EXISTS events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                time REAL NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, data BLOB NOT NULL);
            PRAGMA user_version=1;
        ''')
        for kind, data in self.db.execute('SELECT kind,data FROM records'):
            value = decode(bytes(data))
            self._publish({(kind, value['id']): value})
        self.writer = threading.Thread(target=self._write, name='weave-persistence', daemon=True)
        self.writer.start()

    @staticmethod
    def _field(record, field):
        return bool(record.get(field)) if field == 'released' else record.get(field)

    def _publish(self, changes):
        with self.cache_lock:
            for (kind, identity), value in changes.items():
                old = self.records[kind].get(identity)
                for field in self.FIELDS:
                    index = self.index[kind][field]
                    if old is not None:
                        key = self._field(old, field)
                        index[key].discard(identity)
                        if not index[key]:
                            del index[key]
                    index[self._field(value, field)].add(identity)
                self.records[kind][identity] = value

    @contextmanager
    def transaction(self, *, deferred=False):
        with self.lock:
            current = getattr(self.local, 'transaction', None)
            if current is not None:
                yield self
                return
            transaction = {'changes': {}, 'events': [], 'deferred': deferred}
            self.local.transaction = transaction
            try:
                if self.error:
                    raise RuntimeError('controller persistence failed') from self.error
                if self.closed:
                    raise RuntimeError('controller state is closed')
                yield self
                changes = transaction['changes']
                if changes:
                    rows = [(kind, v['id'], v.get('state'), v.get('parent'), v.get('worker'),
                             v['created'], v['updated'], v['version'], encode(v))
                            for (kind, _), v in changes.items()]
                    future = self._enqueue(rows, transaction['events'])
                    if not transaction['deferred'] or future.done():
                        future.result()
                    self._publish(changes)
            finally:
                self.local.transaction = None

    def require_durable(self):
        """Upgrade a recoverable observation when it discovers a state change."""
        self.local.transaction['deferred'] = False

    @contextmanager
    def read(self):
        """Read related published records consistently without a writer lock.

        This section must not mutate state or perform I/O. Transactions publish
        their complete change set under the same brief memory lock.
        """
        with self.cache_lock:
            yield self

    def _enqueue(self, rows=(), events=(), operation=None):
        future = Future()
        with self.condition:
            if self.closed:
                future.set_exception(RuntimeError('controller state is closed'))
            elif self.error:
                future.set_exception(RuntimeError('controller persistence failed'))
            else:
                self.pending.append((rows, events, operation, future))
                self.condition.notify()
        return future

    def _write(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.pending or self.closed)
                if not self.pending:
                    return
                batch = list(self.pending)
                self.pending.clear()
            try:
                # Repeated observations coalesce; audit events remain ordered.
                rows, events = {}, []
                for values, messages, _, _ in batch:
                    rows.update(((v[0], v[1]), v) for v in values)
                    events.extend(messages)
                if rows or events:
                    self.db.execute('BEGIN IMMEDIATE')
                    try:
                        self.db.executemany('''INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(kind,id) DO UPDATE SET state=excluded.state,
                            parent=excluded.parent, worker=excluded.worker, updated=excluded.updated,
                            version=excluded.version, data=excluded.data''', rows.values())
                        self.db.executemany('INSERT INTO events(time,kind,id,data) VALUES (?,?,?,?)', events)
                        self.db.execute('COMMIT')
                    except BaseException:
                        self.db.execute('ROLLBACK')
                        raise
            except BaseException as error:
                with self.condition:
                    self.error = error
                    batch.extend(self.pending)
                    self.pending.clear()
                for _, _, _, future in batch:
                    if not future.done():
                        future.set_exception(error)
                continue
            for _, _, operation, future in batch:
                try:
                    future.set_result(operation(self.db) if operation else None)
                except Exception as error:
                    future.set_exception(error)

    def get(self, kind, identity, *, required=True, fields=None):
        transaction = getattr(self.local, 'transaction', None)
        value = transaction['changes'].get((kind, identity)) if transaction else None
        if value is None:
            with self.cache_lock:
                value = self.records.get(kind, {}).get(identity)
        if value is None:
            if required:
                raise FileNotFoundError(f'{kind} does not exist: {identity}')
            return None
        return clone(project(value, fields))

    def _select(self, kind, filters):
        with self.cache_lock:
            records = self.records.get(kind, {})
            indexes = [self.index[kind][field].get(value, set()) for field, value in filters.items()]
            if indexes:
                smallest = min(indexes, key=len)
                values = {i: records[i] for i in smallest if all(i in index for index in indexes)}
            else:
                values = dict(records)
        transaction = getattr(self.local, 'transaction', None)
        if transaction:
            for (k, identity), value in transaction['changes'].items():
                if k == kind:
                    values.pop(identity, None)
                    if all(self._field(value, field) == expected for field, expected in filters.items()):
                        values[identity] = value
        return values.values()

    def list(self, kind, *, state=None, parent=None, worker=None, owner=None, released=None, fields=None):
        filters = {k: v for k, v in dict(state=state, parent=parent, worker=worker,
                                        owner=owner, released=released).items() if v is not None}
        values = self._select(kind, filters)
        return [clone(project(v, fields)) for v in sorted(values, key=lambda v: (v['created'], v['id']))]

    def put(self, kind, record, *, expected=None, event=None):
        with self.transaction():
            previous = self.get(kind, record['id'], required=False)
            version = previous['version'] if previous else 0
            if expected is not None and version != expected:
                raise RuntimeError('record changed during operation: ' + record['id'])
            now = time.time()
            value = clone({**record, 'created': previous['created'] if previous else now,
                           'updated': now, 'version': version + 1})
            self.local.transaction['changes'][(kind, value['id'])] = value
            if event is not None:
                self.local.transaction['events'].append((now, kind, value['id'], encode(event)))
            return clone(value)

    def metadata(self, kind, *, parent=None, limit=None, _cache=None):
        values = self._select(kind, {'parent': parent} if parent is not None else {})
        values = sorted(values, key=lambda v: (-v['created'], v['id']))
        selected = values if limit is None else values[:limit]
        if _cache is None:
            return [clone(v, metadata=True) for v in selected]
        # The monitor owns this cache and treats these snapshots as immutable.
        # Retained history must not be copied again on every sampling interval.
        result = []
        for value in selected:
            previous = _cache.get(value['id'])
            if previous is None or previous['version'] != value['version']:
                previous = _cache[value['id']] = clone(value, metadata=True)
            result.append(previous)
        return result

    def event_page(self, clauses=(), values=(), *, limit=100, reverse=False):
        # History reads have their own connection and never hold record locks.
        reader = sqlite3.connect((self.root / 'state.sqlite').as_uri() + '?mode=ro', uri=True, timeout=5)
        try:
            return reader.execute('SELECT sequence,time,kind,id,data FROM events' +
                (' WHERE ' + ' AND '.join(clauses) if clauses else '') +
                ' ORDER BY sequence ' + ('DESC' if reverse else 'ASC') + ' LIMIT ?',
                [*values, limit]).fetchall()
        finally:
            reader.close()

    def events(self, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('invalid event cursor or limit')
        rows = self.event_page(['sequence>?'], [after], limit=limit)
        return [dict(sequence=r[0], time=r[1], kind=r[2], id=r[3], detail=decode(bytes(r[4]))) for r in rows]

    def backup(self, destination):
        destination = Path(destination).expanduser().resolve()
        if destination.exists():
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        def save(database):
            with sqlite3.connect(destination) as other:
                database.backup(other)
        try:
            with self.lock:
                self._enqueue(operation=save).result()
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return str(destination)

    def close(self):
        with self.lock:
            if self.closed:
                return
            with self.condition:
                self.closed = True
                self.condition.notify()
            self.writer.join()
            self.db.close()
            self.authority.close()
            if self.error:
                raise RuntimeError('controller persistence failed') from self.error

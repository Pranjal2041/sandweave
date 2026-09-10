"""Transactional controller records. A process lock protects the single writer."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import sqlite3
import threading
import time

from ..sandbox.wire import encode, decode


class State:
    def __init__(self, directory):
        self.root = Path(directory).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.authority = (self.root / 'controller.lock').open('a')
        try:
            fcntl.flock(self.authority, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.authority.close()
            raise RuntimeError('a controller already owns ' + str(self.root)) from None
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / 'state.sqlite', isolation_level=None,
                                  check_same_thread=False, timeout=30)
        os.chmod(self.root / 'state.sqlite', 0o600)
        # One controller holds the connection for its lifetime. Rollback mode
        # avoids WAL's cross-process shared-memory requirement on shared storage.
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1):
            self.close()
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

    @contextmanager
    def transaction(self):
        with self.lock:
            if self.db.in_transaction:
                # All nesting is within the same thread while holding the lock.
                yield self
                return
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield self
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
            else:
                self.db.execute('COMMIT')

    def get(self, kind, identity, *, required=True):
        with self.lock:
            row = self.db.execute('SELECT data FROM records WHERE kind=? AND id=?',
                                  (kind, identity)).fetchone()
        if row is None:
            if required:
                raise FileNotFoundError(f'{kind} does not exist: {identity}')
            return None
        return decode(bytes(row[0]))

    def list(self, kind, *, state=None, parent=None, worker=None):
        clauses, values = ['kind=?'], [kind]
        for name, value in (('state', state), ('parent', parent), ('worker', worker)):
            if value is not None:
                clauses.append(name + '=?')
                values.append(value)
        with self.lock:
            rows = self.db.execute('SELECT data FROM records WHERE ' + ' AND '.join(clauses) +
                                   ' ORDER BY created, id', values).fetchall()
        return [decode(bytes(row[0])) for row in rows]

    def put(self, kind, record, *, expected=None, event=None):
        with self.transaction():
            previous = self.get(kind, record['id'], required=False)
            version = previous['version'] if previous else 0
            if expected is not None and version != expected:
                raise RuntimeError('record changed during operation: ' + record['id'])
            now = time.time()
            value = {**record, 'created': previous['created'] if previous else now,
                     'updated': now, 'version': version + 1}
            self.db.execute('''INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(kind,id) DO UPDATE SET state=excluded.state,
                parent=excluded.parent, worker=excluded.worker, updated=excluded.updated,
                version=excluded.version, data=excluded.data''',
                (kind, value['id'], value.get('state'), value.get('parent'), value.get('worker'),
                 value['created'], now, value['version'], encode(value)))
            if event is not None:
                self.db.execute('INSERT INTO events(time,kind,id,data) VALUES (?,?,?,?)',
                                (now, kind, value['id'], encode(event)))
            return value

    def events(self, after=0, limit=100):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('invalid event cursor or limit')
        with self.lock:
            rows = self.db.execute('SELECT sequence,time,kind,id,data FROM events '
                                   'WHERE sequence>? ORDER BY sequence LIMIT ?', (after, limit)).fetchall()
        return [dict(sequence=r[0], time=r[1], kind=r[2], id=r[3], detail=decode(bytes(r[4]))) for r in rows]

    def backup(self, destination):
        destination = Path(destination).expanduser().resolve()
        if destination.exists():
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            with self.lock, sqlite3.connect(destination) as other:
                self.db.backup(other)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return str(destination)

    def close(self):
        with self.lock:
            self.db.close()
            self.authority.close()

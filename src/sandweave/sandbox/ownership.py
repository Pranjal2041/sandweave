"""Process ownership, enforced by the worker even after a client crashes."""
import json
import logging
import os
from pathlib import Path
import threading
import time
import uuid

from .connection import Connection
from .errors import OwnerExpired
from .workspace import atomic_json

HEARTBEAT_SECONDS = 5
GRACE_SECONDS = 30


def process_scope():
    try:
        return {'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'pid_namespace': os.readlink('/proc/self/ns/pid')}
    except OSError:
        return None


def process_state(pid):
    # comm may contain spaces and parentheses; fields after its closing ')' are
    # state (field 3) through starttime (field 22) and beyond.
    fields = Path(f'/proc/{pid}/stat').read_bytes().rsplit(b')', 1)[1].split()
    return fields[0].decode('ascii'), fields[19].decode('ascii')


def process_identity():
    scope = process_scope()
    try:
        _, started = process_state(os.getpid())
    except (OSError, IndexError):
        return None
    return {'pid': os.getpid(), 'started': started, 'scope': scope} if scope else None


def process_alive(identity):
    """True/False only for a process we can identify locally; None otherwise."""
    if not identity or identity['scope'] != process_scope():
        return None
    try:
        state, started = process_state(identity['pid'])
    except FileNotFoundError:
        return False
    except (OSError, IndexError):
        return None
    return started == identity['started'] and state not in ('Z', 'X', 'x')


class Owners:
    """Private persisted leases. Heartbeats never wait for a sandbox operation."""
    def __init__(self, root):
        self.root = Path(root) / 'owners'
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.records = {}

    def _path(self, identity):
        if not isinstance(identity, str) or len(identity) != 32 or any(c not in '0123456789abcdef' for c in identity):
            raise ValueError('invalid owner ID')
        return self.root / (identity + '.json')

    def _read(self, identity):
        if identity not in self.records:
            self.records[identity] = json.loads(self._path(identity).read_text())
        return self.records[identity]

    def _write(self, record):
        atomic_json(self._path(record['id']), record)
        self.records[record['id']] = record

    def register(self, identity, process):
        with self.lock:
            path = self._path(identity)
            if process is not None:
                if (not isinstance(process, dict) or type(process.get('pid')) is not int
                        or process['pid'] <= 0 or not isinstance(process.get('started'), str)
                        or not process['started'].isdigit() or not isinstance(process.get('scope'), dict)):
                    raise ValueError('invalid owner process identity')
            if path.exists():
                if self._read(identity)['process'] != process:
                    raise ValueError('owner ID is already in use')
                return self.heartbeat(identity)
            self._write({'id': identity, 'process': process,
                         'expires_at': time.time() + GRACE_SECONDS})
            return {'heartbeat_seconds': HEARTBEAT_SECONDS, 'grace_seconds': GRACE_SECONDS}

    def reason(self, identity):
        if identity is None:
            return None
        with self.lock:
            try:
                record = self._read(identity)
            except FileNotFoundError:
                return 'owner_lost'
            if record.get('reason'):
                return record['reason']
            alive = process_alive(record['process'])
            reason = ('owner_exited' if alive is False else
                      'owner_heartbeat_expired' if alive is None and time.time() >= record['expires_at'] else None)
            if reason:
                # Expiry is final. A late heartbeat must not resurrect a lease
                # after cleanup has already started for one of its sandboxes.
                expired = {**record, 'reason': reason}
                self.records[identity] = expired
                try:
                    self._write(expired)
                except OSError:
                    # A full metadata disk must not prevent releasing a dead
                    # owner's resources. The saved deadline is already expired.
                    logging.getLogger(__name__).exception('Could not persist expired owner %s', identity)
            return reason

    def heartbeat(self, identity):
        with self.lock:
            reason = self.reason(identity)
            if reason:
                raise OwnerExpired('sandbox owner is no longer active: ' + reason)
            self._write({**self._read(identity), 'expires_at': time.time() + GRACE_SECONDS})
            return {'heartbeat_seconds': HEARTBEAT_SECONDS, 'grace_seconds': GRACE_SECONDS}


class ClientOwner:
    def __init__(self, connection):
        self.id = uuid.uuid4().hex
        self.connection = connection.clone(timeout=2)
        self.pid = os.getpid()
        self.expired = False
        try:
            settings = self.connection.call('owner_register', identity=self.id, process=process_identity())
        except BaseException:
            self.connection.close()
            raise
        self.interval = settings['heartbeat_seconds']
        self.thread = threading.Thread(target=self._heartbeat, name='sandweave-owner', daemon=True)
        self.thread.start()

    def _heartbeat(self):
        while os.getpid() == self.pid:
            time.sleep(self.interval)
            try:
                self.connection.call('owner_heartbeat', identity=self.id)
            except OwnerExpired:
                self.expired = True
                self.connection.close()
                return
            except Exception:
                # A transport failure is allowed to recover during the grace
                # period. Only the worker decides when ownership has expired.
                self.connection.close()


_clients = {}
_clients_lock = threading.Lock()


def client_owner(connection):
    # One independent heartbeat channel per worker and Python process, shared
    # by all sandboxes and pool threads. close()/GC of a handle does not end it.
    key = (connection.host, connection.port, connection.token, connection.unix_path)
    with _clients_lock:
        owner = _clients.get(key)
        if owner is None or owner.expired:
            owner = _clients[key] = ClientOwner(connection)
        return owner.id


def _after_fork():
    global _clients, _clients_lock
    # A forked child may create its own sandboxes, but must never renew its
    # parent's leases or inherit a mutex held by a vanished thread.
    _clients = {}
    _clients_lock = threading.Lock()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork)

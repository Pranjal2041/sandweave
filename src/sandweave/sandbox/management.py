"""Durable assignments and sandbox-scoped access on an existing worker."""
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time

from .errors import OperationUnknown, UnsupportedFeature
from .wire import decode, encode


class Management:
    def __init__(self, worker):
        self.worker = worker
        self.root = worker.root / 'assignments'
        self.root.mkdir(exist_ok=True, mode=0o700)
        from .telemetry import Sampler
        self.sampler = Sampler(worker.root)

    def path(self, identity):
        self.worker.path(identity)  # Use the same ID validation as the executor.
        return self.root / (identity + '.bin')

    def read(self, identity):
        path = self.path(identity)
        return decode(path.read_bytes()) if path.exists() else None

    def write(self, value):
        fd, temporary = tempfile.mkstemp(dir=self.root, prefix='.assignment-')
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(encode(value))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path(value['id']))
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def apply(self, identity, cluster, generation, action, *, spec=None, operation_id=None,
              reference=None, cache_key=None, refresh=False, owner=None, process=None):
        if type(generation) is not int or generation < 1:
            raise ValueError('assignment generation must be positive')
        if action not in ('create', 'claim', 'terminate'):
            raise ValueError('unknown assignment action')
        intent = dict(action=action, spec=spec, operation_id=operation_id, reference=reference,
                      cache_key=cache_key, refresh=refresh, owner=owner, process=process)
        stamp = hashlib.sha256(encode(intent)).hexdigest()
        canonical = hashlib.sha256(encode(intent, canonical=True)).hexdigest()
        with self.worker.lock(identity):
            previous = self.read(identity)
            if previous is None and self.worker.path(identity).exists():
                raise PermissionError('existing sandbox is not owned by this management assignment')
            if previous is None and action == 'claim':
                raise FileNotFoundError('cannot claim an unassigned sandbox')
            if previous:
                if previous['cluster'] != cluster:
                    raise PermissionError('sandbox belongs to another controller')
                if generation < previous['generation']:
                    raise OperationUnknown('assignment was superseded', sandbox_id=identity)
                matches = (previous['canonical_intent'] == canonical if 'canonical_intent' in previous
                           else previous['intent'] == stamp)
                if generation == previous['generation'] and not matches:
                    raise ValueError('assignment generation already has a different operation')
                if previous['action'] == 'terminate' and action != 'terminate':
                    raise OperationUnknown('terminated assignment cannot be resurrected', sandbox_id=identity)
            value = dict(id=identity, cluster=cluster, generation=generation, action=action,
                         intent=stamp, canonical_intent=canonical,
                         token=previous['token'] if previous else secrets.token_hex(32),
                         owner=owner if action in ('claim', 'create') else (previous or {}).get('owner'))
            # Persist a cancellation even if launch has not reached the worker.
            # A delayed create then fails its generation/tombstone check.
            self.write(value)
            if action == 'create':
                if spec is None:
                    raise ValueError('creation requires a sandbox definition')
                if owner:
                    self.worker.owner_register(owner, process)
                if self.worker.path(identity).exists():
                    record = self.worker.read(identity)
                    if record.get('operation_id') != operation_id:
                        raise FileExistsError('sandbox ID belongs to another operation')
                    result = self.worker.describe(identity)
                    if result['state'] in ('creating', 'preparing'):
                        # The original worker may have died during preparation.
                        # It is unsafe to restart setup with unknown side effects.
                        raise OperationUnknown('worker creation was interrupted; inspect or terminate this attempt',
                                               sandbox_id=identity, operation_id=operation_id)
                else:
                    result = self.worker.create(spec, identity, operation_id=operation_id,
                        reference=reference, cache_key=cache_key, refresh=refresh, owner=owner)
            elif action == 'claim':
                result = self.worker.describe(identity)
                if result['state'] != 'ready':
                    raise OperationUnknown('pool member is no longer ready', sandbox_id=identity)
                if owner:
                    self.worker.owner_register(owner, process)
                record = self.worker.read(identity)
                record['owner'] = owner
                record['spec']['detached'] = owner is None
                if spec is not None:
                    record['spec']['ttl'] = spec.get('ttl')
                    if spec.get('ttl') is not None:
                        record['expires_at'] = time.time() + spec['ttl']
                    else:
                        record.pop('expires_at', None)
                self.worker.write(record)
                result = self.worker.describe(identity)
            elif self.worker.path(identity).exists():
                result = self.worker.terminate(identity)
            else:
                result = {'id': identity, 'state': 'terminated'}
            return {'sandbox': result, 'token': self.token(value), 'generation': generation}

    @staticmethod
    def token(value):
        return 'sw1.' + value['id'] + '.' + value['token']

    def authorize(self, token, operation, parameters):
        if not isinstance(token, str) or not token.startswith('sw1.'):
            return False
        parts = token.split('.')
        if len(parts) != 3:
            return False
        try:
            value = self.read(parts[1])
        except (OSError, ValueError):
            return False
        if value is None or not hmac.compare_digest(parts[2], value['token']):
            return False
        if operation == 'owner_heartbeat':
            return value.get('owner') is not None and parameters.get('identity') == value['owner']
        if operation in ('snapshot_info', 'snapshot_spec', 'snapshot_verify'):
            try:
                return self.worker.store.resolve(parameters['reference'])['source'] == value['id']
            except (KeyError, OSError, RuntimeError):
                return False
        if operation not in {'describe', 'command_start', 'process_status', 'process_output',
                             'process_stdin', 'process_terminate', 'process_resize', 'file', 'setup',
                             'pause', 'resume', 'terminate', 'capture', 'stop', 'control'}:
            return False
        return parameters.get('identity') == value['id']

    def inventory(self):
        from .admission import reservation
        from .ownership import process_scope
        import socket
        gpus = []
        try:
            import gvisor_gpu
            for index in gvisor_gpu.eligible_devices():
                info = gvisor_gpu.device_identity(index)
                model = subprocess.check_output(['nvidia-smi', '-i', info['uuid'], '--query-gpu=name',
                    '--format=csv,noheader'], text=True, timeout=10).strip()
                gpus.append({'uuid': info['uuid'], 'model': model, 'index': index})
        except (ImportError, OSError, RuntimeError, subprocess.SubprocessError):
            pass
        live = []
        records = self.worker.list()
        for record in records:
            if record['state'] in ('terminated', 'stopped') or record.get('cleanup_complete'):
                continue
            if record['runtime_status']['status'] not in ('running', 'paused', 'starting') and record['state'] not in ('creating', 'preparing'):
                continue
            assignment = self.read(record['id'])
            live.append({'id': record['id'], 'memory': reservation(record['spec']),
                         'gpu': bool(record['spec']['resources']['gpu']),
                         'gpu_uuids': [g['uuid'] for g in record['runtime_status'].get('gpus', []) or [] if g.get('uuid')],
                         'cluster': assignment['cluster'] if assignment else None})
        return {'protocol': 1, 'hostname': socket.gethostname(), 'pid': os.getpid(),
                'scope': process_scope(), 'workspace': str(self.worker.root),
                'cpus': sorted(os.sched_getaffinity(0)), 'memory': self.worker.memory_budget,
                'gpus': gpus, 'runtimes': ['gvisor', 'apptainer'], 'live': live,
                'telemetry': self.sampler.sample(records, gpus)}

    def logs(self, identity, stream='launcher', size=65536):
        from .telemetry import tail
        self.worker.path(identity)
        if stream not in ('launcher', 'runtime'):
            raise ValueError('log stream must be launcher or runtime')
        status = self.worker.runtime.status(identity)
        if not status.get('logs'):
            return {'text': '', 'message': 'This runtime does not expose logs.'}
        return tail(Path(status['logs']) / ('launcher.log' if stream == 'launcher' else 'runsc.log'), size)

    def prepare(self, template):
        """Extend this allocation's installation without changing live guests."""
        import importlib
        import importlib.util
        from ..onboarding import validate_assets, python_packages
        from ..installation import needs_helpers
        from .preparation import ensure
        from .targets import local_connection
        from .workspace import tool
        from .errors import ResourceUnavailable
        # Runtime files can be present while this worker's Python environment
        # lacks an optional control dependency. Install it before taking the
        # existing-workspace shortcut; no new worker is needed for imports.
        if (any(importlib.util.find_spec(module) is None for module, _ in python_packages(template)) or
                'vr' in template['capabilities'] and not tool('ffmpeg')):
            ensure(template)
            importlib.invalidate_caches()
        try:
            validate_assets(self.worker.root, template, contents=False)
            complete = not needs_helpers(self.worker.root, template)
        except (OSError, ValueError, KeyError, ResourceUnavailable):
            complete = False
        if complete:
            connection = self.worker.endpoint.connection()
        else:
            # This process retains the worker's allocation, installation paths
            # and CPU/GPU eligibility. Setup chooses a new immutable workspace.
            connection = local_connection(template=template)
        try:
            return {'information': connection.call('ping'), 'token': connection.token}
        finally:
            connection.close()

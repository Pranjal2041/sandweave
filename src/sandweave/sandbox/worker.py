"""Persistent worker for owned sandbox instances and shared engine helpers."""
import argparse
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import threading
import time
import uuid

from .errors import SandboxError, SetupError, UnsupportedFeature, CacheConflict, IncompatibleSnapshot
from .snapshots import Store
from .wire import decode, encode, MAX_BODY
from .workspace import atomic_json, home, prepare


class Worker:
    def __init__(self, root):
        from .runtimes.router import Runtime
        self.root = Path(root)
        self.runtime = Runtime(self.root)
        self.store = Store(self.runtime)
        self.records = self.root / 'sandboxes'
        self.records.mkdir(exist_ok=True)
        self.locks, self.guard, self.controls, self.deadlines = {}, threading.RLock(), {}, {}
        self.stopping = False
        self.pools = {}
        from .admission import budget
        self.memory_budget = budget()
        threading.Thread(target=self.expire, name='sandweave-ttl', daemon=True).start()

    def expire(self):
        while True:
            for path in self.records.glob('*.bin'):
                try:
                    record = self.read(path.stem)
                    if (record['state'] in ('ready', 'paused') and record.get('expires_at') is not None
                            and time.time() >= record['expires_at']):
                        with self.lock(record['id']):
                            record = self.read(record['id'])
                            if record['state'] in ('ready', 'paused'):
                                self.terminate(record['id'])
                                record = self.read(record['id'])
                                record['termination_reason'] = 'ttl'
                                self.write(record)
                except Exception as error:
                    # Leave failed cleanup retryable and retain a diagnostic.
                    atomic_json(self.root / 'ttl-error.json', {'id': path.stem, 'error': str(error), 'time': time.time()})
            time.sleep(.25)

    def lock(self, identity):
        with self.guard:
            return self.locks.setdefault(identity, threading.RLock())

    def path(self, identity):
        import re
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', identity):
            raise ValueError('invalid sandbox ID')
        return self.records / (identity + '.bin')

    def read(self, identity):
        return decode(self.path(identity).read_bytes())

    def write(self, record):
        destination = self.path(record['id'])
        temporary = destination.with_suffix('.tmp')
        temporary.write_bytes(encode(record))
        temporary.chmod(0o600)
        os.replace(temporary, destination)

    def create(self, spec, identity, *, operation_id=None, reference=None, cache_key=None, refresh=False):
        if cache_key is not None:
            from ..templates.resolve import fingerprint
            from .workspace import locked
            # A distinct build lock lets publication retain its own atomic CAS.
            path = self.store.name_path(cache_key)
            with locked(self.store.root / 'locks' / (path.stem + '.build')):
                previous = self.store.alias(cache_key)
                if previous and previous['provenance'] != 'prepared':
                    raise CacheConflict('cache name contains a manual capture: ' + cache_key)
                prepared = json.loads((self.root / 'prepared.json').read_text())
                from ..templates.controls import descriptors
                stamp = fingerprint({k: v for k, v in spec.items()
                                     if k not in ('name', 'ttl', 'startup_timeout', 'keep_on_error')} |
                                    {'engine': prepared['engine_sources_sha256'],
                                     'sdk': prepared.get('sdk_sources_sha256'),
                                     'assets': prepared.get('assets_sha256'),
                                     'controls': descriptors(spec['template']),
                                     'runtime': (self.root / 'tools/gvisor-socket/runtime.json').read_text()})
                if not refresh and previous and previous['fingerprint'] == stamp:
                    return self._create(spec, identity, operation_id=operation_id, reference=previous['id'])
                result = self._create(spec, identity, operation_id=operation_id)
                try:
                    saved = self.capture(identity, state='filesystem')
                    self.store.publish(cache_key, saved, (previous or {}).get('id'),
                                       provenance='prepared', fingerprint=stamp)
                except BaseException:
                    if not spec.get('keep_on_error'):
                        self.terminate(identity)
                    raise
                return result
        return self._create(spec, identity, operation_id=operation_id, reference=reference)

    def _create(self, spec, identity, *, operation_id=None, reference=None):
        started = time.monotonic()
        with self.lock(identity):
            if self.path(identity).exists():
                record = self.read(identity)
                if record.get('operation_id') != operation_id:
                    raise FileExistsError('sandbox ID is already in use')
                if record['state'] == 'ready':
                    return self.describe(identity)
                raise SandboxError('previous creation did not finish', sandbox_id=identity,
                                   operation_id=operation_id, phase=record['state'])
            self.runtime.adapter(spec['runtime'])
            import external_mounts
            spec['mounts'] = external_mounts.normalize(spec.get('mounts', []))
            external_mounts.configure({'mounts': []}, spec['mounts'])
            from ..templates.controls import descriptors
            declared = descriptors(spec['template'])
            record = {'id': identity, 'name': spec.get('name'), 'spec': spec, 'state': 'creating',
                      'operation_id': operation_id, 'created_at': time.time(), 'agent': None,
                      'timings': {}, 'workspace': str(self.root), 'capabilities': declared}
            from .admission import admit
            with self.guard:
                if self.stopping:
                    raise SandboxError('worker is shutting down')
                record['admission'] = admit(self, spec)
                self.write(record)
            try:
                self.deadlines[identity] = started + spec['startup_timeout']
                saved = self.store.resolve(reference) if reference else None
                if saved and saved['state'] == 'memory':
                    for key in ('runtime', 'resources', 'env', 'mounts'):
                        if spec[key] != saved['spec'][key]:
                            raise IncompatibleSnapshot('memory restore cannot change ' + key)
                snapshot = self.store.materialize(saved) if saved else None
                token = saved['agent']['token'] if saved and saved['state'] == 'memory' else None
                record['agent'] = self.runtime.create(identity, {**spec, '_startup_deadline': self.deadlines[identity]},
                                                       snapshot=snapshot, token=token)
                self.remaining(identity)
                record['state'] = 'preparing'
                self.write(record)
                if saved is None:
                    for step in spec['template'].get('setup_steps', []):
                        self.setup(identity, step)
                else:
                    record['restored_from'] = saved['id']
                self.write(record)
                cold = saved is None or saved['state'] == 'filesystem'
                if cold:
                    self.start_services(identity)
                self.attach_controls(identity, cold=cold)
                self.remaining(identity)
                record['state'] = 'ready'
                if spec.get('ttl') is not None:
                    record['expires_at'] = time.time() + spec['ttl']
                record['timings']['ready_seconds'] = time.monotonic() - started
                self.write(record)
                self.deadlines.pop(identity, None)
                return self.describe(identity)
            except BaseException as error:
                self.deadlines.pop(identity, None)
                for key, value in {'sandbox_id': identity, 'operation_id': operation_id,
                                   'phase': record['state']}.items():
                    if getattr(error, key, None) is None:
                        setattr(error, key, value)
                record.update(state='failed', error=str(error))
                self.write(record)
                if not spec.get('keep_on_error'):
                    try:
                        self.detach_controls(identity, reason='terminate')
                        state = self.runtime.status(identity)['status']
                        if state in ('starting', 'running', 'paused'):
                            self.runtime.terminate(identity)
                        record['cleanup_complete'] = True
                    except Exception as cleanup_error:
                        record['cleanup_error'] = str(cleanup_error)
                    self.write(record)
                raise

    def remaining(self, identity, limit=None):
        deadline = self.deadlines.get(identity)
        if deadline is None:
            return limit
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('sandbox startup deadline exceeded: ' + identity)
        return remaining if limit is None else min(limit, remaining)

    def describe(self, identity):
        if not self.path(identity).exists():
            matches = [self.read(path.stem)['id'] for path in self.records.glob('*.bin')
                       if self.read(path.stem).get('name') == identity and
                       self.read(path.stem)['state'] not in ('terminated', 'stopped', 'failed')]
            if len(matches) != 1:
                raise FileNotFoundError('sandbox name is missing or ambiguous: ' + identity)
            identity = matches[0]
        record = self.read(identity)
        status = self.runtime.status(identity)
        if record['state'] in ('ready', 'paused') and status['status'] not in ('running', 'paused'):
            with self.lock(identity):
                record = self.read(identity)
                status = self.runtime.status(identity)
                if record['state'] in ('ready', 'paused') and status['status'] not in ('running', 'paused'):
                    record.update(state='failed', error='runtime was lost or exited outside a lifecycle operation')
                    self.write(record)
        # Secret guest control tokens stay in the worker's private record.
        return {key: value for key, value in {**record, 'runtime_status': status}.items()
                if key not in ('agent',)}

    def list(self):
        return [self.describe(path.stem) for path in sorted(self.records.glob('*.bin'))]

    def agent(self, identity):
        record = self.read(identity)
        diagnostic = record['state'] == 'failed' and record['spec'].get('keep_on_error') and record.get('agent')
        if record['state'] not in ('ready', 'preparing') and not diagnostic:
            raise SandboxError('sandbox is not running: ' + record['state'], sandbox_id=identity)
        return self.runtime.agent(identity, record['agent'])

    def command_start(self, identity, process_id, command=None, argv=None, cwd='/workspace',
                      env=None, user=None, timeout=None, shell=None, max_output_bytes=None, pty=False):
        spec = self.read(identity)['spec']
        if (command is None) == (argv is None):
            raise ValueError('provide exactly one command string or argv')
        if command is not None:
            if not isinstance(command, str) or '\0' in command:
                raise ValueError('command must be a string without NUL characters')
            argv = [shell or spec['template'].get('command_shell', '/bin/sh'), '-c', command]
        elif shell is not None:
            raise ValueError('shell cannot be combined with argv')
        return self.agent(identity).call('spawn', identity=process_id, argv=argv, cwd=cwd,
                                         env={**spec.get('env', {}), **(env or {})},
                                         user=user or spec['template'].get('user', 'root'), timeout=timeout,
                                         max_output_bytes=max_output_bytes if max_output_bytes is not None else
                                         spec['template'].get('runtime_options', {}).get('max_output_bytes', 64*1024**2), pty=pty)

    def process_status(self, identity, process_id):
        return self.agent(identity).call('status', identity=process_id)

    def process_output(self, identity, process_id, **params):
        return self.agent(identity).call('output', identity=process_id, **params)

    def process_stdin(self, identity, process_id, **params):
        return self.agent(identity).call('stdin', identity=process_id, **params)

    def process_terminate(self, identity, process_id):
        return self.agent(identity).call('terminate', identity=process_id)

    def process_resize(self, identity, process_id, rows, cols):
        return self.agent(identity).call('resize', identity=process_id, rows=rows, cols=cols)

    def file(self, identity, **params):
        return self.agent(identity).call('file', **params)

    def setup(self, identity, step):
        directory = '/opt/sandweave/setup/' + uuid.uuid4().hex
        for name, data in step['files'].items():
            path = Path(name)
            if path.is_absolute() or '..' in path.parts:
                raise ValueError('setup input escapes its declared context')
            for offset in range(0, max(1, len(data)), 1024**2):
                self.file(identity, op='write', path=directory+'/'+name, data=data[offset:offset+1024**2],
                          offset=offset, truncate=offset == 0, mode=0o755 if name == step['script'] else 0o644)
        process = uuid.uuid4().hex
        script = directory + '/' + step['script']
        content = step['files'][step['script']]
        argv = [script] if content.startswith(b'#!') else ['/bin/sh', script]
        self.command_start(identity, process, argv=argv, cwd=directory,
                           user=step.get('user', 'root'), timeout=self.remaining(identity, step.get('timeout')))
        while (status := self.process_status(identity, process))['returncode'] is None:
            time.sleep(.02)
        if status['returncode']:
            stderr = self.process_output(identity, process, stream='stderr', size=65536).decode(errors='replace')
            raise SetupError(f'setup exited with {status["returncode"]}: {stderr}', sandbox_id=identity)
        return status

    def snapshot_info(self, reference):
        return self.store.public(self.store.resolve(reference))

    def snapshot_spec(self, reference):
        saved = self.store.resolve(reference)
        return {'reference': saved['id'], 'spec': saved['spec']}

    def snapshot_verify(self, reference):
        return self.store.verify(self.store.resolve(reference))

    def capture(self, identity, state='memory', key=None, experimental_gpu_live=False):
        previous = self.store.alias(key) if key is not None else None
        if previous and previous['provenance'] != 'captured':
            raise CacheConflict('cache name contains a preparation build: ' + key)
        record = self.read(identity)
        if state == 'memory' and record['spec']['resources']['gpu']:
            if not experimental_gpu_live or record['spec']['template']['capabilities']:
                raise UnsupportedFeature('live GPU graphics checkpoints are unsupported; CUDA-only capture requires experimental_gpu_live=True')
        if experimental_gpu_live and (state != 'memory' or not record['spec']['resources']['gpu']):
            raise ValueError('experimental GPU live capture requires GPU memory state')
        revision = 'snap-' + uuid.uuid4().hex
        self.detach_controls(identity, reason='snapshot')
        try:
            saved = self.runtime.capture(identity, revision, state, experimental_gpu_live=experimental_gpu_live)
        finally:
            if record['state'] == 'ready':
                self.attach_controls(identity, cold=False)
        metadata = self.store.record(revision, saved, record)
        if key is not None:
            self.store.publish(key, metadata, (previous or {}).get('id'))
        return self.store.public(metadata)

    def stop(self, identity, state='auto', experimental_gpu_live=False):
        record = self.read(identity)
        if record['state'] == 'stopped' and record.get('checkpoint'):
            return self.snapshot_info(record['checkpoint'])
        previous = record['state']
        self.pause(identity)
        try:
            saved = self.capture(identity, state=state, experimental_gpu_live=experimental_gpu_live)
        except BaseException:
            if previous == 'ready':
                self.resume(identity)
            raise
        self.terminate(identity)
        record = self.read(identity)
        record.update(state='stopped', checkpoint=saved['id'])
        self.write(record)
        return saved

    def pause(self, identity):
        record = self.read(identity)
        self.detach_controls(identity, reason='pause')
        self.runtime.pause(identity)
        record['state'] = 'paused'
        self.write(record)
        return self.describe(identity)

    def resume(self, identity):
        record = self.read(identity)
        self.runtime.resume(identity)
        record['state'] = 'ready'
        self.write(record)
        self.attach_controls(identity, cold=False)
        return self.describe(identity)

    def terminate(self, identity):
        record = self.read(identity)
        self.detach_controls(identity, reason='terminate')
        if self.runtime.status(identity)['status'] in ('running', 'paused', 'starting'):
            self.runtime.terminate(identity)
        record['state'] = 'terminated'
        self.write(record)
        return self.describe(identity)

    def start_services(self, identity):
        from ..templates.controls import Context
        context = Context(self, identity)
        spec = self.read(identity)['spec']
        for name, service in spec['template']['services'].items():
            self.remaining(identity)
            command = service['command']
            process = uuid.uuid4().hex
            self.command_start(identity, process,
                               command=command if isinstance(command, str) else None,
                               argv=command if isinstance(command, list) else None,
                               user=service.get('user'), env=service.get('env'),
                               cwd=service.get('cwd', '/workspace'))
            self.process_stdin(identity, process, close=True)
            ready = service.get('ready', {}).get('exec')
            if ready:
                deadline = time.monotonic() + self.remaining(identity, service.get('ready_timeout', 120))
                while True:
                    if self.process_status(identity, process)['returncode'] is not None:
                        raise SetupError('template service exited before readiness: ' + name)
                    result = context.run(argv=ready if isinstance(ready, list) else None,
                                         command=ready if isinstance(ready, str) else None,
                                         user=service.get('user', spec['template']['user']),
                                         timeout=5, check=False)
                    if result['returncode'] == 0:
                        break
                    if time.monotonic() >= deadline:
                        raise SetupError('template service readiness timed out: ' + name)
                    time.sleep(.1)

    def attach_controls(self, identity, *, cold=False):
        from ..templates.controls import Context, provider
        attached = self.controls.setdefault(identity, {})
        for name, config in self.read(identity)['spec']['template']['capabilities'].items():
            if name not in attached:
                attached[name] = provider(config.get('provider', name)).attach(Context(self, identity), config, cold=cold)
            elif hasattr(attached[name], 'reattach'):
                attached[name].reattach()

    def detach_controls(self, identity, *, reason):
        attached = self.controls.get(identity, {})
        for name in list(attached):
            retained = attached[name].detach(reason)
            if not retained:
                del attached[name]

    def control(self, identity, name, method, parameters):
        if self.read(identity)['state'] != 'ready':
            raise SandboxError('controls require a ready sandbox', sandbox_id=identity)
        self.attach_controls(identity)
        if name not in self.controls[identity]:
            raise UnsupportedFeature('template does not provide controls: ' + name)
        return self.controls[identity][name].call(method, parameters)

    def pool(self, action, name=None, arguments=None, lease_id=None):
        from .pool import Pool
        from .resources import CPU, GPU, Memory
        if action == 'list':
            return [self.pool('status', key) for key in list(self.pools)]
        if action == 'create':
            options = dict(arguments['options'])
            for key, kind in (('cpu', CPU), ('memory', Memory), ('gpu', GPU)):
                if isinstance(options.get(key), dict):
                    options[key] = kind(**options[key])
            pool = Pool(size=arguments['size'], warm=arguments['warm'], target=self.endpoint, **options)
            with self.guard:
                if name in self.pools or self.stopping:
                    raise FileExistsError('named pool already exists or worker is stopping')
                self.pools[name] = {'pool': pool, 'leases': {}}
            try:
                pool.start()
            except BaseException:
                pool.close()
                self.pools.pop(name)
                raise
        if name not in self.pools:
            raise FileNotFoundError('named pool is not running on this worker: ' + str(name))
        record = self.pools[name]
        pool = record['pool']
        if action in ('status', 'create'):
            with pool.condition:
                return {'name': name, 'size': pool.size, 'warm': pool.warm, 'ready': len(pool.idle),
                        'active': len(pool.active), 'pending': pool.pending,
                        'sandboxes': sorted(env.id for env in pool.all), 'closed': pool.closed}
        if action == 'checkout':
            lease = pool.acquire()
            env = lease.__enter__()
            identity = uuid.uuid4().hex
            with self.guard:
                record['leases'][identity] = lease
            return {'lease_id': identity, 'id': env.id}
        if action == 'release':
            with self.guard:
                lease = record['leases'].pop(lease_id, None)
            if lease is not None:
                lease.__exit__(None, None, None)
            return None
        if action == 'close':
            pool.close()
            with self.guard:
                self.pools.pop(name, None)
            return {'closed': True, 'name': name}
        raise ValueError('unknown pool operation')

    def dispatch(self, operation, parameters):
        if operation == '_debug_threads':
            import sys
            import traceback
            return {str(identity): ''.join(traceback.format_stack(frame))
                    for identity, frame in sys._current_frames().items()}
        if operation == '_shutdown_if_idle':
            with self.guard:
                if self.pools:
                    raise RuntimeError('worker still owns named pools')
                if any(self.read(p.stem)['state'] in ('creating', 'preparing') or
                       self.runtime.status(p.stem)['status'] in ('running', 'paused', 'starting')
                       for p in self.records.glob('*.bin')):
                    raise RuntimeError('worker still owns live sandboxes')
                self.stopping = True
                self.mark_stopping()
            threading.Thread(target=self.shutdown, daemon=True).start()
            return {'stopping': True}
        allowed = {'create', 'describe', 'list', 'command_start', 'process_status', 'process_output',
                   'process_stdin', 'process_terminate', 'file', 'setup', 'pause', 'resume', 'terminate',
                   'snapshot_info', 'snapshot_spec', 'snapshot_verify', 'capture', 'stop', 'control'}
        allowed.add('pool')
        allowed.add('process_resize')
        if operation == 'ping':
            return {'hostname': socket.gethostname(), 'pid': os.getpid(), 'workspace': str(self.root),
                    'cpu_affinity': sorted(os.sched_getaffinity(0)), 'memory_budget': self.memory_budget}
        if operation not in allowed:
            raise UnsupportedFeature('unknown worker operation: ' + operation)
        identity = parameters.get('identity')
        if identity and operation != 'describe':
            with self.lock(identity):
                return getattr(self, operation)(**parameters)
        return getattr(self, operation)(**parameters)


def serve(metadata_path):
    from .workspace import tool_path, home
    os.environ['PATH'] = tool_path()
    # Existing workers retain their storage when a later setup changes the
    # client's saved location. Managed tools have already been added to PATH.
    os.environ['SANDWEAVE_HOME'] = str(home())
    root = prepare()
    # An attached worker keeps the asset source it prepared, even after setup
    # changes the client's default selection for future workers.
    os.environ['SANDWEAVE_ASSETS'] = json.loads((root / 'prepared.json').read_text())['assets']
    authority = (root / '.worker.lock').open('a')
    try:
        fcntl.flock(authority, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        authority.close()
        raise RuntimeError('another worker already owns this resource workspace') from None
    worker = Worker(root)
    token = secrets.token_hex(32)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        # Flush the small RPC header and body together. Splitting them across
        # forwarded TCP streams can incur a delayed ACK for each small reply.
        wbufsize = 64 * 1024

        def setup(self):
            super().setup()
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        def log_message(self, *args):
            pass

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get('X-Sandweave-Token', ''), token):
                self.send_error(403)
                return
            try:
                length = int(self.headers.get('Content-Length', '-1'))
                if not 0 <= length <= MAX_BODY:
                    raise ValueError('invalid request size')
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError('incomplete request')
                request = decode(body)
                result = {'result': worker.dispatch(request['op'], request.get('params', {}))}
            except Exception as error:
                result = {'error': {'kind': type(error).__name__, 'message': str(error),
                                    **{key: getattr(error, key, None) for key in
                                       ('operation_id', 'sandbox_id', 'phase')}}}
            payload = encode(result)
            self.send_response(200)
            self.send_header('Content-Type', 'application/vnd.sandweave.frame')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    from .targets import Endpoint
    worker.endpoint = Endpoint(server.server_port, token)
    worker.shutdown = server.shutdown
    information = {'hostname': socket.gethostname(), 'pid': os.getpid(), 'status': 'ready',
                   'port': server.server_port, 'token': token, 'workspace': str(root)}
    worker.mark_stopping = lambda: atomic_json(metadata_path, {**information, 'status': 'stopping'})
    atomic_json(metadata_path, information)
    try:
        server.serve_forever(poll_interval=.1)
    finally:
        server.server_close()
        authority.close()
        if metadata_path.exists() and json.loads(metadata_path.read_text()).get('pid') == os.getpid():
            metadata_path.unlink()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--metadata', required=True)
    args = parser.parse_args()
    serve(Path(args.metadata))


if __name__ == '__main__':
    main()

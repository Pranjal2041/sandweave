"""Persistent worker for owned sandbox instances and shared engine helpers."""
import argparse
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
        self.locks, self.guard, self.controls = {}, threading.RLock(), {}

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
                stamp = fingerprint({k: v for k, v in spec.items()
                                     if k not in ('name', 'ttl', 'startup_timeout', 'keep_on_error')} |
                                    {'engine': (self.root / 'prepared.json').read_text(),
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
            if spec.get('mounts'):
                raise UnsupportedFeature('explicit mount support is not yet configured')
            from ..templates.controls import descriptors
            declared = descriptors(spec['template'])
            record = {'id': identity, 'name': spec.get('name'), 'spec': spec, 'state': 'creating',
                      'operation_id': operation_id, 'created_at': time.time(), 'agent': None,
                      'timings': {}, 'workspace': str(self.root), 'capabilities': declared}
            self.write(record)
            try:
                saved = self.store.resolve(reference) if reference else None
                if saved and saved['state'] == 'memory':
                    for key in ('runtime', 'resources', 'env', 'mounts'):
                        if spec[key] != saved['spec'][key]:
                            raise IncompatibleSnapshot('memory restore cannot change ' + key)
                snapshot = self.store.materialize(saved) if saved else None
                token = saved['agent']['token'] if saved and saved['state'] == 'memory' else None
                record['agent'] = self.runtime.create(identity, spec, snapshot=snapshot, token=token)
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
                record['state'] = 'ready'
                record['timings']['ready_seconds'] = time.monotonic() - started
                self.write(record)
                return self.describe(identity)
            except BaseException as error:
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
                      env=None, user=None, timeout=None, shell=None):
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
                                         user=user or spec['template'].get('user', 'root'), timeout=timeout)

    def process_status(self, identity, process_id):
        return self.agent(identity).call('status', identity=process_id)

    def process_output(self, identity, process_id, **params):
        return self.agent(identity).call('output', identity=process_id, **params)

    def process_stdin(self, identity, process_id, **params):
        return self.agent(identity).call('stdin', identity=process_id, **params)

    def process_terminate(self, identity, process_id):
        return self.agent(identity).call('terminate', identity=process_id)

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
                           user=step.get('user', 'root'), timeout=step.get('timeout'))
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

    def capture(self, identity, state='memory', key=None):
        previous = self.store.alias(key) if key is not None else None
        if previous and previous['provenance'] != 'captured':
            raise CacheConflict('cache name contains a preparation build: ' + key)
        record = self.read(identity)
        if state == 'memory' and record['spec']['resources']['gpu']:
            raise UnsupportedFeature('live GPU graphics checkpoints are unsupported; choose filesystem state')
        revision = 'snap-' + uuid.uuid4().hex
        self.detach_controls(identity, reason='snapshot')
        try:
            saved = self.runtime.capture(identity, revision, state)
        finally:
            if record['state'] == 'ready':
                self.attach_controls(identity, cold=False)
        metadata = self.store.record(revision, saved, record)
        if key is not None:
            self.store.publish(key, metadata, (previous or {}).get('id'))
        return self.store.public(metadata)

    def stop(self, identity, state='auto'):
        record = self.read(identity)
        if record['state'] == 'stopped' and record.get('checkpoint'):
            return self.snapshot_info(record['checkpoint'])
        previous = record['state']
        self.pause(identity)
        try:
            saved = self.capture(identity, state=state)
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
                deadline = time.monotonic() + service.get('ready_timeout', 120)
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

    def dispatch(self, operation, parameters):
        if operation == '_shutdown_if_idle':
            if any(self.runtime.status(p.stem)['status'] in ('running', 'paused', 'starting')
                   for p in self.records.glob('*.bin')):
                raise RuntimeError('worker still owns live sandboxes')
            threading.Thread(target=self.shutdown, daemon=True).start()
            return {'stopping': True}
        allowed = {'create', 'describe', 'list', 'command_start', 'process_status', 'process_output',
                   'process_stdin', 'process_terminate', 'file', 'setup', 'pause', 'resume', 'terminate',
                   'snapshot_info', 'snapshot_spec', 'snapshot_verify', 'capture', 'stop', 'control'}
        if operation == 'ping':
            return {'hostname': socket.gethostname(), 'pid': os.getpid(), 'workspace': str(self.root)}
        if operation not in allowed:
            raise UnsupportedFeature('unknown worker operation: ' + operation)
        identity = parameters.get('identity')
        if identity:
            with self.lock(identity):
                return getattr(self, operation)(**parameters)
        return getattr(self, operation)(**parameters)


def serve(metadata_path):
    root = prepare()
    worker = Worker(root)
    token = secrets.token_hex(32)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

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
    worker.shutdown = server.shutdown
    atomic_json(metadata_path, {'hostname': socket.gethostname(), 'pid': os.getpid(),
                               'port': server.server_port, 'token': token, 'workspace': str(root)})
    server.serve_forever(poll_interval=.1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--metadata', required=True)
    args = parser.parse_args()
    serve(Path(args.metadata))


if __name__ == '__main__':
    main()

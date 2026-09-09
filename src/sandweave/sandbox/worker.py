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

from .errors import SandboxError, SetupError, UnsupportedFeature
from .wire import decode, encode, MAX_BODY
from .workspace import atomic_json, home, prepare


class Worker:
    def __init__(self, root):
        from .runtimes.gvisor.driver import Runtime
        self.root = Path(root)
        self.runtime = Runtime(self.root)
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

    def create(self, spec, identity, *, operation_id=None):
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
            if spec['runtime'] != 'gvisor':
                raise UnsupportedFeature('runtime has not been registered: ' + spec['runtime'])
            if spec.get('mounts'):
                raise UnsupportedFeature('explicit mount support is not yet configured')
            record = {'id': identity, 'name': spec.get('name'), 'spec': spec, 'state': 'creating',
                      'operation_id': operation_id, 'created_at': time.time(), 'agent': None,
                      'timings': {}, 'workspace': str(self.root)}
            self.write(record)
            try:
                record['agent'] = self.runtime.create(identity, spec)
                record['state'] = 'preparing'
                self.write(record)
                for step in spec['template'].get('setup_steps', []):
                    self.setup(identity, step)
                record['state'] = 'ready'
                record['timings']['ready_seconds'] = time.monotonic() - started
                self.write(record)
                return self.describe(identity)
            except BaseException as error:
                record.update(state='failed', error=str(error))
                self.write(record)
                if not spec.get('keep_on_error'):
                    try:
                        state = self.runtime.status(identity)['status']
                        if state in ('starting', 'running', 'paused'):
                            self.runtime.terminate(identity)
                        record['cleanup_complete'] = True
                    except Exception as cleanup_error:
                        record['cleanup_error'] = str(cleanup_error)
                    self.write(record)
                raise

    def describe(self, identity):
        record = self.read(identity)
        status = self.runtime.status(identity)
        # Secret guest control tokens stay in the worker's private record.
        return {key: value for key, value in {**record, 'runtime_status': status}.items()
                if key not in ('agent',)}

    def list(self):
        return [self.describe(path.stem) for path in sorted(self.records.glob('*.bin'))]

    def agent(self, identity):
        record = self.read(identity)
        if record['state'] not in ('ready', 'preparing'):
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

    def pause(self, identity):
        record = self.read(identity)
        self.runtime.pause(identity)
        record['state'] = 'paused'
        self.write(record)
        return self.describe(identity)

    def resume(self, identity):
        record = self.read(identity)
        self.runtime.resume(identity)
        record['state'] = 'ready'
        self.write(record)
        return self.describe(identity)

    def terminate(self, identity):
        record = self.read(identity)
        if self.runtime.status(identity)['status'] in ('running', 'paused', 'starting'):
            self.runtime.terminate(identity)
        record['state'] = 'terminated'
        self.write(record)
        return self.describe(identity)

    def dispatch(self, operation, parameters):
        if operation == '_shutdown_if_idle':
            if any(self.runtime.status(p.stem)['status'] in ('running', 'paused', 'starting')
                   for p in self.records.glob('*.bin')):
                raise RuntimeError('worker still owns live sandboxes')
            threading.Thread(target=self.shutdown, daemon=True).start()
            return {'stopping': True}
        allowed = {'create', 'describe', 'list', 'command_start', 'process_status', 'process_output',
                   'process_stdin', 'process_terminate', 'file', 'setup', 'pause', 'resume', 'terminate'}
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

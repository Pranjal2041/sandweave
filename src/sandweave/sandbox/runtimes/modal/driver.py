"""Run each Sandweave sandbox as one Modal sandbox under Modal's own gVisor.

The worker keeps the same records, controls and guest agent protocol. The agent
runs inside the Modal sandbox and is reached through a TLS tunnel with the
sandbox's token. Engine features (memory snapshots, pause, guest clocks, audit,
network policies, CPU weights) have no Modal equivalent and are rejected.
"""
import json
import os
from pathlib import Path
import secrets
import time

from ...connection import Connection
from ...errors import ResourceUnavailable, UnsupportedFeature
from ...resources import memory_bytes
from ...workspace import atomic_json

AGENT_PORT = 23799
VR_PORT = 23800
STATE = '/run/sandweave'
MAXIMUM_LIFETIME = 24 * 3600


def app_name():
    return os.environ.get('SANDWEAVE_MODAL_APP', 'sandweave')


class Runtime:
    name = 'modal'

    def __init__(self, root):
        self.root = Path(root)
        self.connections, self.metadata = {}, {}

    @staticmethod
    def descriptor():
        return {'name': 'modal', 'state': [], 'guest_privilege': 'Modal sandbox root',
                'cpu': 'Modal CPU reservation', 'memory': 'Modal memory reservation',
                'network': 'Modal network (internet or blocked)', 'isolation': "Modal's gVisor"}

    def _record_path(self, identity):
        return self.root / 'sandboxes' / (identity + '.modal.json')

    def _sandbox(self, identity):
        import modal
        record = json.loads(self._record_path(identity).read_text())
        return modal.Sandbox.from_id(record['sandbox_id'])

    @staticmethod
    def resources(spec):
        resources = spec['resources']
        if spec.get('mounts'):
            raise UnsupportedFeature('runtime="modal" does not mount worker directories')
        if resources['cpu']['weight'] != 100 or resources['cpu']['quota'] is not None:
            raise UnsupportedFeature('runtime="modal" reserves whole CPUs; weights and quotas are unavailable')
        if resources['network']['mode'] not in ('internet', 'offline') or resources['network']['allow_cidrs']:
            raise UnsupportedFeature('runtime="modal" supports network="internet" or "offline"')
        gpu = None
        if resources['gpu']:
            if resources['gpu'].get('sm_chunks') is not None:
                raise UnsupportedFeature('runtime="modal" allocates whole GPUs')
            model = resources['gpu'].get('model') or os.environ.get('SANDWEAVE_MODAL_GPU', 'L40S')
            gpu = list(model) if isinstance(model, (list, tuple)) else model
        memory = resources['memory']
        total = memory_bytes(memory['guest']) + memory_bytes(memory['runtime'])
        return {'cpu': float(resources['cpu']['vcpus']), 'memory': (total + 1024**2 - 1) // 1024**2,
                'gpu': gpu, 'block_network': resources['network']['mode'] == 'offline'}

    def create(self, identity, spec, *, snapshot=None, token=None):
        import modal
        from .image import prepare
        if snapshot is not None:
            raise UnsupportedFeature('runtime="modal" cannot restore Sandweave snapshots')
        deadline = spec.get('_startup_deadline', time.monotonic() + spec.get('startup_timeout', 300))
        token = token or secrets.token_hex(32)
        app = modal.App.lookup(app_name(), create_if_missing=True)
        image = prepare(spec['template'], self.root, app)
        lifetime = min(MAXIMUM_LIFETIME, int(spec['ttl'])) if spec.get('ttl') else MAXIMUM_LIFETIME
        options = self.resources(spec)
        sandbox = modal.Sandbox.create('python3', '-u', '/opt/sandweave/guest_boot.py', app=app, image=image,
                                       timeout=lifetime, encrypted_ports=[AGENT_PORT, VR_PORT],
                                       env={'SANDWEAVE_GUEST_USER': spec['template'].get('user', 'ga')},
                                       **options)
        atomic_json(self._record_path(identity), {'sandbox_id': sandbox.object_id, 'app': app_name(),
                                                  'created_at': time.time()})
        try:
            self._wait_boot(sandbox, deadline)
            self._start_agent(sandbox, token)
            tunnels = sandbox.tunnels(timeout=max(1, int(deadline - time.monotonic())))
            agent, vr = tunnels[AGENT_PORT], tunnels[VR_PORT]
            metadata = {'sandbox_id': sandbox.object_id, 'host': agent.host, 'port': agent.port, 'token': token,
                        'vr': {'host': vr.host, 'port': vr.port}}
            client = Connection(agent.host, agent.port, token, tls=True, timeout=10)
            while True:
                try:
                    client.call('ping')
                    break
                except Exception:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('guest command service did not become ready: ' + identity)
                    time.sleep(.5)
            client.close()
            self.connections[identity] = Connection(agent.host, agent.port, token, tls=True, timeout=30)
            self.metadata[identity] = metadata
            return metadata
        except BaseException:
            if not spec.get('keep_on_error'):
                sandbox.terminate()
            raise

    @staticmethod
    def _read(sandbox, path):
        process = sandbox.exec('cat', path, timeout=30)
        output = process.stdout.read()
        return output if process.wait() == 0 else None

    def _wait_boot(self, sandbox, deadline):
        while True:
            if sandbox.poll() is not None:
                raise RuntimeError('Modal sandbox exited during boot: ' + sandbox.stderr.read()[-4000:])
            state = self._read(sandbox, STATE + '/boot.json')
            if state:
                state = json.loads(state)
                if state.get('ready'):
                    return state
                if state.get('error'):
                    raise RuntimeError('guest boot failed: ' + state['error'])
            if time.monotonic() >= deadline:
                raise TimeoutError('Modal sandbox did not finish booting')
            time.sleep(1)

    @staticmethod
    def _start_agent(sandbox, token):
        from ..gvisor.driver import agent_source
        sandbox.filesystem.write_bytes(agent_source(token_file=True).encode(), STATE + '/agent.py')
        sandbox.filesystem.write_bytes((token + '\n').encode(), STATE + '/agent.token')
        command = (f'chmod 600 {STATE}/agent.token {STATE}/agent.py && '
                   f'setsid python3 -I -S -u {STATE}/agent.py {AGENT_PORT} {STATE}/agent.token '
                   f'> {STATE}/logs/agent.log 2>&1 < /dev/null &')
        process = sandbox.exec('sh', '-c', command, timeout=60)
        if process.wait():
            raise RuntimeError('guest agent did not start: ' + process.stderr.read())

    def agent(self, identity, metadata):
        self.metadata[identity] = metadata
        if identity not in self.connections:
            self.connections[identity] = Connection(metadata['host'], metadata['port'], metadata['token'],
                                                    tls=True, timeout=30)
        return self.connections[identity]

    def vr_connection(self, identity, *, timeout=30):
        metadata = self.metadata.get(identity)
        if metadata is None:
            from ...wire import decode
            metadata = decode((self.root / 'sandboxes' / (identity + '.bin')).read_bytes())['agent']
        return Connection(metadata['vr']['host'], metadata['vr']['port'], metadata['token'], tls=True,
                          timeout=timeout, rpc_path='/vr')

    def vr_stream(self, context, **options):
        from .vr import RemoteVRStream
        return RemoteVRStream(self, context, **options)

    def detach(self, identity):
        connection = self.connections.pop(identity, None)
        if connection:
            connection.close()

    def status(self, identity):
        if not self._record_path(identity).is_file():
            return {'status': 'missing', 'gpus': []}
        try:
            returncode = self._sandbox(identity).poll()
        except Exception as error:
            return {'status': 'unknown', 'error': str(error), 'gpus': None}
        return {'status': 'running' if returncode is None else 'stopped', 'returncode': returncode, 'gpus': []}

    def pause(self, identity):
        raise UnsupportedFeature('runtime="modal" cannot pause a sandbox')

    def resume(self, identity):
        raise UnsupportedFeature('runtime="modal" cannot pause a sandbox')

    def capture(self, identity, label, state, *, experimental_gpu_live=False):
        raise UnsupportedFeature('runtime="modal" does not support Sandweave snapshots')

    def terminate(self, identity):
        self.detach(identity)
        if self._record_path(identity).is_file():
            self._sandbox(identity).terminate()
        return {'status': 'stopped'}

    def discard(self, identity):
        if self.status(identity)['status'] == 'running':
            raise ResourceUnavailable('cannot discard a live sandbox')
        self._record_path(identity).unlink(missing_ok=True)

    def delete(self, identity):
        self.discard(identity)

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
import threading
import time

from ...connection import Connection
from ...errors import ResourceUnavailable, UnsupportedFeature
from ...resources import memory_bytes
from ...workspace import atomic_json

AGENT_PORT = 23799
IO_PORT = 23801
STATE = '/run/sandweave'
MAXIMUM_LIFETIME = 24 * 3600


SCHEDULE_FLOOR_S = 60  # after a long first image build, scheduling still gets this long


def app_name():
    return os.environ.get('SANDWEAVE_MODAL_APP', 'sandweave')


def placement():
    """Place sandboxes in the worker's own Modal region and cloud.

    Every frame fetch and acknowledged input costs one worker-to-guest round trip,
    so distance bounds their rates (notes/modal-runtime.md). SANDWEAVE_MODAL_REGION
    (comma-separated) and SANDWEAVE_MODAL_CLOUD override the worker's own; "any"
    leaves that choice to Modal. Modal charges a multiplier for a pinned region.
    """
    worker_cloud = os.environ.get('MODAL_CLOUD_PROVIDER', '').removeprefix('CLOUD_PROVIDER_').lower()
    if worker_cloud == 'unspecified':  # Modal did not say which cloud runs the worker: pin only the region
        worker_cloud = ''
    region = os.environ.get('SANDWEAVE_MODAL_REGION', os.environ.get('MODAL_REGION'))
    cloud = os.environ.get('SANDWEAVE_MODAL_CLOUD', worker_cloud)
    return {**({'region': region.split(',')} if region and region != 'any' else {}),
            **({'cloud': cloud} if cloud and cloud != 'any' else {})}


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
            if isinstance(model, (list, tuple)):
                if len(model) != 1:
                    raise UnsupportedFeature('Modal sandboxes take a single GPU model, e.g. GPU(model="L40S")')
                model = model[0]
            gpu = model
        memory = resources['memory']
        total = memory_bytes(memory['guest']) + memory_bytes(memory['runtime'])
        return {'cpu': float(resources['cpu']['vcpus']), 'memory': (total + 1024**2 - 1) // 1024**2,
                'gpu': gpu, 'block_network': resources['network']['mode'] == 'offline', **placement()}

    @staticmethod
    def _schedule(modal, app, identity, image, lifetime, spec, options, deadline):
        """modal.Sandbox.create waits until Modal schedules the sandbox, which never happens while the requested
        GPU has no capacity in the pinned region. Bound it by the startup deadline: the sandbox is named after its
        Sandweave identity, so one still unscheduled at the deadline is found and terminated, and the caller gets
        TimeoutError (and can ask for another GPU model)."""
        name = 'sandweave-' + identity
        outcome, lock, cancelled = {}, threading.Lock(), threading.Event()

        def launch():
            try:
                sandbox = modal.Sandbox.create(
                    'python3', '-u', '/opt/sandweave/guest_boot.py', app=app, image=image, name=name,
                    timeout=lifetime, encrypted_ports=[AGENT_PORT, IO_PORT],
                    env={'SANDWEAVE_GUEST_USER': spec['template'].get('user', 'ga')}, **options)
            except BaseException as error:
                outcome['error'] = error
                return
            with lock:
                if not cancelled.is_set():
                    outcome['sandbox'] = sandbox
                    return
            # Scheduled only after the caller gave up: nobody owns it, so it must not run (and bill) unattended.
            sandbox.terminate()

        thread = threading.Thread(target=launch, name='sandweave-modal-create', daemon=True)
        thread.start()
        thread.join(max(0.0, deadline - time.monotonic()))
        with lock:
            if 'sandbox' not in outcome and 'error' not in outcome:
                cancelled.set()
        if cancelled.is_set():
            # A still-pending create is usually not yet findable by name; launch() terminates it if Modal
            # returns it later. Terminate it now where Modal already resolves the name.
            try:
                modal.Sandbox.from_name(app_name(), name).terminate()
            except Exception:
                pass
            raise TimeoutError(f"Modal did not schedule the sandbox before its startup deadline (gpu {options.get('gpu')}, "
                               f"region {options.get('region')}, cloud {options.get('cloud')})")
        if 'error' in outcome:
            raise outcome['error']
        return outcome['sandbox']

    def create(self, identity, spec, *, snapshot=None, token=None):
        import modal
        from .image import prepare
        if snapshot is not None:
            raise UnsupportedFeature('runtime="modal" cannot restore Sandweave snapshots')
        deadline = spec.get('_startup_deadline', time.monotonic() + spec.get('startup_timeout', 300))
        token = token or secrets.token_hex(32)
        app = modal.App.lookup(app_name(), create_if_missing=True)
        image = prepare(spec['template'], self.root, app)
        # Build first: a first image build is not a wait for capacity and must not consume the scheduling deadline.
        image.build(app)
        lifetime = min(MAXIMUM_LIFETIME, int(spec['ttl'])) if spec.get('ttl') else MAXIMUM_LIFETIME
        options = self.resources(spec)
        sandbox = self._schedule(modal, app, identity, image, lifetime, spec, options,
                                 max(deadline, time.monotonic() + SCHEDULE_FLOOR_S))
        atomic_json(self._record_path(identity), {'sandbox_id': sandbox.object_id, 'app': app_name(),
                                                  'created_at': time.time()})
        try:
            boot = self._wait_boot(sandbox, deadline)
            self._start_agent(sandbox, token)
            tunnels = sandbox.tunnels(timeout=max(1, int(deadline - time.monotonic())))
            agent, relay = tunnels[AGENT_PORT], tunnels[IO_PORT]
            metadata = {'sandbox_id': sandbox.object_id, 'host': agent.host, 'port': agent.port, 'token': token,
                        'io': {'host': relay.host, 'port': relay.port},
                        'placement': {'region': boot.get('region'), 'cloud': boot.get('cloud'),
                                      'worker_region': os.environ.get('MODAL_REGION')}}
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
                   f'> {STATE}/logs/agent.log 2>&1 < /dev/null & '
                   f'setsid python3 -u /opt/sandweave/guest_io.py {IO_PORT} {STATE}/agent.token '
                   f'> {STATE}/logs/guest-io.log 2>&1 < /dev/null &')
        process = sandbox.exec('sh', '-c', command, timeout=60)
        if process.wait():
            raise RuntimeError('guest agent did not start: ' + process.stderr.read())

    def agent(self, identity, metadata):
        self.metadata[identity] = metadata
        if identity not in self.connections:
            self.connections[identity] = Connection(metadata['host'], metadata['port'], metadata['token'],
                                                    tls=True, timeout=30)
        return self.connections[identity]

    def io_connection(self, identity, *, timeout=30, wait=0):
        """A persistent stream to guest_io.py for frames, input, haptics and audio.

        The relay starts with the agent; `wait` seconds allow for it to begin listening.
        """
        from .io_client import IOConnection
        metadata = self.metadata.get(identity)
        if metadata is None:
            from ...wire import decode
            metadata = decode((self.root / 'sandboxes' / (identity + '.bin')).read_bytes())['agent']
        deadline = time.monotonic() + wait
        while True:
            try:
                return IOConnection(metadata['io']['host'], metadata['io']['port'], metadata['token'], timeout=timeout)
            except (OSError, ConnectionError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.5)

    def vr_stream(self, context, **options):
        from .vr import RemoteVRStream
        return RemoteVRStream(self, context, **options)

    def audio_stream(self, context, *, user='ga', server=None, sink=None):
        """Start the guest's audio capture (of another PulseAudio server and sink when given);
        returns the connection that reads it."""
        connection = self.io_connection(context.id, timeout=30, wait=context.remaining(60))
        try:
            connection.call('audio_start', user=user, server=server, sink=sink)
        except BaseException:
            connection.close()
            raise
        return connection

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

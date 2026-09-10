"""A persistent worker agent that reaches the controller through outbound RPCs."""
import argparse
from collections import OrderedDict
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid

from .client import ClusterConnection
from ..sandbox.connection import Connection
from ..sandbox.errors import ResourceUnavailable
from ..sandbox.ownership import process_alive, process_identity
from ..sandbox.resources import positive, memory_bytes
from ..sandbox.workspace import home, atomic_json, locked, engine_sources, software_identity

LOG = logging.getLogger(__name__)


def limits(cpus=None, gpus=None, memory=None):
    """Apply only inside the new agent, before it creates worker processes."""
    available = sorted(os.sched_getaffinity(0))
    if cpus is not None:
        positive(cpus, 'cpus', integer=True)
        os.sched_setaffinity(0, available[:cpus])
    if gpus is not None:
        if type(gpus) is not int or gpus < 0:
            raise ValueError('gpus must be a nonnegative integer')
        if gpus:
            sys.path.insert(0, str(engine_sources()))
            import gvisor_gpu
            try:
                devices = gvisor_gpu.eligible_devices()
            except (FileNotFoundError, subprocess.CalledProcessError):
                devices = []
        else:
            devices = []
        os.environ['SANDWEAVE_GPU_LIMIT'] = ','.join(str(i) for i in devices[:gpus])
    if memory is not None:
        from ..sandbox.admission import budget
        os.environ['SANDWEAVE_MEMORY_BUDGET'] = str(min(budget(), memory_bytes(memory))) + 'B'


class Bridge:
    def __init__(self, config, channel, *, connections=32):
        self.config, self.channel, self.count = config, channel, connections
        self.stopping = threading.Event()
        self.threads = []

    def start(self):
        for _ in range(self.count):
            thread = threading.Thread(target=self._run, daemon=True, name='weave-forward')
            thread.start()
            self.threads.append(thread)
        return self

    def _run(self):
        controller = None
        worker_connections = OrderedDict()
        response = None
        try:
            while not self.stopping.is_set():
                try:
                    if controller is None:
                        controller = ClusterConnection(self.config, timeout=30)
                    if response is not None:
                        controller.control.call('relay_result', channel=self.channel, **response)
                        response = None
                    request = controller.control.call('relay_poll', channel=self.channel)
                    if request is None:
                        continue
                    try:
                        endpoint = request['endpoint']
                        if endpoint['hostname'] != socket.gethostname() or endpoint.get('relay') != self.channel:
                            raise PermissionError('worker channel cannot forward to another machine')
                        key = (endpoint['port'], endpoint['token'])
                        if key not in worker_connections:
                            if len(worker_connections) >= 4:
                                worker_connections.popitem(last=False)[1].close()
                            worker_connections[key] = Connection('127.0.0.1', *key)
                        worker_connections.move_to_end(key)
                        result = {'result': worker_connections[key].call(request['method'], **request['parameters'])}
                    except Exception as error:
                        result = {'error': {'kind': type(error).__name__, 'message': str(error),
                            **{k: getattr(error, k, None) for k in ('operation_id', 'sandbox_id', 'phase')}}}
                    response = {'identity': request['id'], 'response': result}
                except Exception:
                    # Keep a completed result until acknowledged, but never
                    # execute its command again after losing the reply.
                    LOG.debug('Worker channel reconnecting', exc_info=True)
                    if controller:
                        controller.close()
                        controller = None
                    self.stopping.wait(1)
        finally:
            if controller:
                controller.close()
            for connection in worker_connections.values():
                connection.close()

    def close(self):
        self.stopping.set()


def start(config, *, cpus=None, gpus=None, memory=None, slots=None, labels=None):
    if cpus is not None:
        positive(cpus, 'cpus', integer=True)
    if gpus is not None and (type(gpus) is not int or gpus < 0):
        raise ValueError('gpus must be a nonnegative integer')
    if memory is not None:
        memory_bytes(memory)
    if slots is not None:
        positive(slots, 'slots', integer=True)
    settings = dict(config=config, cpus=cpus, gpus=gpus, memory=memory, slots=slots, labels=labels or {})
    # Capture credentials now; an existing agent must not accidentally join a
    # different authority after the launching shell changes its environment.
    if 'url' in config:
        from .transport import credential
        settings['config'] = {**config, 'token': credential(config)}
        ca_file = config.get('ca_file') or os.environ.get('SANDWEAVE_CA_FILE')
        if ca_file:
            settings['config']['ca_file'] = str(Path(ca_file).expanduser().resolve())
    stamp = hashlib.sha256(json.dumps({**settings, 'software': software_identity(),
        'affinity': sorted(os.sched_getaffinity(0)),
        'allocation': {k: os.environ.get(k) for k in ('SLURM_JOB_ID', 'SLURM_STEP_GPUS', 'SLURM_JOB_GPUS',
             'SANDWEAVE_GPU_DEVICES', 'SANDWEAVE_GPU_LIMIT', 'CUDA_VISIBLE_DEVICES', 'NVIDIA_VISIBLE_DEVICES',
             'SANDWEAVE_MEMORY_BUDGET', 'SLURM_MEM_PER_NODE', 'SLURM_MEM_PER_CPU')}}, sort_keys=True).encode()).hexdigest()[:20]
    directory = home() / 'agents' / stamp
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = directory / 'agent.json'
    with locked(directory / 'startup.lock'):
        if marker.exists():
            previous = json.loads(marker.read_text())
            if process_alive(previous['process']) is not False:
                return previous
        registration = directory / 'worker.json'
        registration.unlink(missing_ok=True)
        settings['channel'] = json.loads((directory / 'settings.json').read_text())['channel'] if (directory / 'settings.json').exists() else uuid.uuid4().hex
        atomic_json(directory / 'settings.json', settings)
        with (directory / 'agent.log').open('ab') as log:
            child = subprocess.Popen([sys.executable, '-m', 'sandweave.weave.worker', '--directory', str(directory)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                env={**os.environ, 'SANDWEAVE_HOME': str(home()),
                     'PYTHONPATH': str(Path(__file__).resolve().parents[2]) + os.pathsep + os.environ.get('PYTHONPATH', '')})
        # Installation may take time; publish the agent immediately so Ctrl+C
        # and a second join do not create competing agents.
        deadline = time.monotonic() + 30
        while not marker.exists() or json.loads(marker.read_text())['pid'] != child.pid:
            if child.poll() is not None:
                raise ResourceUnavailable('worker agent failed: ' + (directory / 'agent.log').read_text()[-4000:])
            if time.monotonic() >= deadline:
                raise TimeoutError('worker agent is still starting; inspect ' + str(directory / 'agent.log'))
            time.sleep(.05)
        return json.loads(marker.read_text())


def serve(directory):
    settings = json.loads((directory / 'settings.json').read_text())
    information = {'pid': os.getpid(), 'process': process_identity(), 'state': 'preparing',
                   'directory': str(directory), 'log': str(directory / 'agent.log'),
                   'limits': {k: settings[k] for k in ('cpus', 'gpus', 'memory', 'slots')}}
    atomic_json(directory / 'agent.json', information)
    bridge = None
    try:
        limits(settings['cpus'], settings['gpus'], settings['memory'])
        from ..sandbox.targets import local_connection
        from ..templates.resolve import Template
        local = local_connection(template=Template('coding').resolve())
        try:
            ping = local.call('ping')
            endpoint = {k: ping[k] for k in ('hostname', 'port', 'workspace')}
            endpoint.update(token=local.token, relay=settings['channel'])
        finally:
            local.close()
        bridge = Bridge(settings['config'], settings['channel']).start()
        signal.signal(signal.SIGTERM, lambda *a: bridge.close())
        signal.signal(signal.SIGINT, lambda *a: bridge.close())
        controller = ClusterConnection(settings['config'])
        try:
            from ..sandbox.errors import OperationUnknown
            while not bridge.stopping.is_set():
                try:
                    worker = controller.control.call('worker_add', target={'endpoint': endpoint},
                        slots=settings['slots'], memory=None, labels=settings['labels'])
                    break
                except OperationUnknown:
                    bridge.stopping.wait(1)
            else:
                return
            information.update(state='ready', worker=worker)
            atomic_json(directory / 'worker.json', worker)
            atomic_json(directory / 'agent.json', information)
            while not bridge.stopping.wait(5):
                try:
                    workers = controller.control.call('worker_list')
                    current = next((w for w in workers if w['id'] == worker['id']), None)
                    if current is not None:
                        information.update(worker=current)
                        atomic_json(directory / 'agent.json', information)
                    if current and current['state'] == 'removed':
                        break
                except Exception:
                    continue  # Pollers reconnect independently; no re-registration or new guests.
        finally:
            controller.close()
    except BaseException as error:
        information.update(state='failed', error=str(error))
        atomic_json(directory / 'agent.json', information)
        raise
    finally:
        if bridge:
            bridge.close()
        if information['state'] == 'ready':
            atomic_json(directory / 'agent.json', {**information, 'state': 'stopped'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', required=True, type=Path)
    serve(parser.parse_args().directory)

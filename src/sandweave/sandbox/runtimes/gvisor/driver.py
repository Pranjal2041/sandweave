"""Adapter around qualified engine operations, run only inside the private worker."""
import importlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time

from ...connection import Connection
from ...errors import ResourceUnavailable, UnsupportedFeature
from ...resources import memory_bytes
from ...workspace import locked, atomic_json
from ...timings import measure

AGENT_PORT = 23799


def agent_source(*, image=False):
    from ... import wire, guest_agent
    wire_source = Path(wire.__file__).read_text()
    source = Path(guest_agent.__file__).read_text()
    return ("import sys, types\nm=types.ModuleType('sandweave_wire')\n"
            f"exec({wire_source!r}, m.__dict__)\nsys.modules['sandweave_wire']=m\n" + source +
            f"\nmain(sys.argv[1], sys.argv[2], image={image!r})\n")


class Runtime:
    def __init__(self, workspace):
        self.root = Path(workspace)
        # This namespace belongs to a separate worker process, never the SDK host.
        sys.path.insert(0, str(self.root / 'scripts'))
        self.engine = importlib.import_module('environment')
        self.manager = self.engine.EnvironmentManager(self.root)
        self.connections = {}

    def gpu(self, requested, *, device_uuid=None):
        if requested is None:
            return None
        gpu_module = importlib.import_module('gvisor_gpu')
        allocation = gpu_module.eligible_devices()
        if not allocation:
            raise ResourceUnavailable('no GPU devices are allocated to this worker')
        selected = None
        for index in allocation:
            gpu_module.allocated_device(index)
            identity = gpu_module.device_identity(index)
            if device_uuid and identity['uuid'] != device_uuid:
                continue
            model = subprocess.check_output(['nvidia-smi', '-i', identity['uuid'], '--query-gpu=name',
                                             '--format=csv,noheader'], text=True).strip()
            if not requested.get('model') or requested['model'].lower() in model.lower():
                selected = index
                break
        if selected is None:
            raise ResourceUnavailable('no allocated GPU matches ' + str(requested.get('model')))
        with locked(self.root / 'tools/gpu/.driver.lock'):
            destination = self.root / 'tools/gpu/driver'
            destination.parent.chmod(0o755)
            if not destination.exists():
                gpu_module.stage_driver(destination)
            destination.chmod(0o755)
            (destination / 'driver.json').chmod(0o644)
            for directory in destination.rglob('*'):
                if directory.is_dir() and not directory.is_symlink():
                    directory.chmod(0o755)
            # Rendering launchers and CUDA checkpoint tools are immutable assets.
            from ...workspace import assets, stage_tree
            source = assets() / 'tools/gpu'
            if source.is_dir():
                for path in source.iterdir():
                    if path.name == 'driver':
                        continue
                    dest = destination.parent / path.name
                    stage_tree(path, dest)
        return selected

    def options(self, spec):
        registry = self.root / 'sandweave-assets.json'
        if registry.is_file():
            from ....onboarding import workload
            installed = json.loads(registry.read_text()).get('workloads')
            required = workload(spec['template'])
            if installed is not None and required not in installed:
                raise ResourceUnavailable(required + ' is not installed on this worker. Run '
                                          'sandweave setup --template ' + required)
        resources = spec['resources']
        cpu, memory = resources['cpu'], resources['memory']
        total = memory_bytes(memory['guest'])
        if memory.get('disk') is not None:
            total += memory_bytes(memory['disk'])
        options = ['--guest-cpus', str(cpu['vcpus']), '--cpu-weight', str(cpu['weight']),
                   '--memory-mib', str((total + 1024**2-1)//1024**2),
                   '--runtime-memory-mib', str((memory_bytes(memory['runtime']) + 1024**2-1)//1024**2),
                   '--guest-gs', '--no-runtime-debug', '--forward', str(AGENT_PORT),
                   '--network-policy', resources['network']['mode']]
        if memory.get('disk') is not None:
            options += ['--ram-mib', str((memory_bytes(memory['guest']) + 1024**2-1)//1024**2),
                        '--disk-path', memory['disk_path']]
        else:
            options += ['--no-disk-memory']
        for cidr in resources['network']['allow_cidrs']:
            options += ['--allow-cidr', cidr]
        if cpu['quota'] is not None:
            options += ['--cpu-policy', 'quota', '--cpu-quota', str(cpu['quota'])]
        runtime = spec['template'].get('runtime_options', {})
        if runtime.get('nftables'):
            options.append('--nftables')
        if runtime.get('cgroup'):
            options += ['--cgroup', runtime['cgroup']]
        gpu = self.gpu(resources['gpu'], device_uuid=spec.get('_gpu_uuid'))
        if gpu is not None:
            if {'desktop', 'vr', 'gamepad'} & spec['template']['capabilities'].keys():
                library = self.root / 'tools/gpu/driver/lib'
                missing = [name for name in ('libEGL_nvidia.so.0', 'libGLX_nvidia.so.0')
                           if not (library / name).is_file()]
                if missing:
                    raise ResourceUnavailable('The worker NVIDIA installation lacks graphics libraries: ' +
                                              ', '.join(missing))
            options += ['--gpu', str(gpu)]
            if resources['gpu'].get('sm_chunks') is not None:
                options += ['--experimental-gpu-sm-chunks', str(resources['gpu']['sm_chunks'])]
                if resources['gpu'].get('client_memory'):
                    options += ['--experimental-gpu-client-memory-mib',
                                str(memory_bytes(resources['gpu']['client_memory'])//1024**2)]
        return options

    def create(self, identity, spec, *, snapshot=None, token=None):
        token = token or secrets.token_hex(32)
        deadline = spec.get('_startup_deadline', time.monotonic() + spec.get('startup_timeout', 300))
        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError('runtime startup deadline exceeded: ' + identity)
            return value
        options = self.options(spec)
        path = self.root / 'sandboxes' / (identity + '.mounts.json')
        atomic_json(path, spec.get('mounts', []))
        options += ['--mounts', str(path)]
        if snapshot is None and spec.get('image'):
            options += ['--base-image', spec['image']['base_image']]
        if snapshot is None and not spec.get('image') and spec['template'].get('base_snapshot'):
            from ...workspace import assets
            from ...snapshots import Store
            base = assets()
            registry = json.loads((base / 'sandweave-assets.json').read_text())
            source = registry['snapshots'][spec['template']['base_snapshot']]
            relative = Path(source['path'])
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('base snapshot must be inside the asset directory')
            path = base / relative
            if source.get('kind') == 'image':
                from ...workspace import _immutable
                image_info = registry['images'][str(relative)]
                _immutable(path, self.root / relative, sha256=image_info['sha256'])
                options += ['--base-image', str(relative)]
            else:
                manifest = json.loads((path / 'snapshot-manifest.json').read_text())
                if manifest['snapshot_id'] != source['snapshot_id'] or manifest['kind'] != 'filesystem':
                    raise ValueError('template base snapshot identity does not match its asset registry')
                snapshot = Store(self).materialize({'id': 'snap-' + source['snapshot_id'],
                                                   'workspace': str(base), 'location': str(path)})
        init = spec['template'].get('runtime_options', {}).get('init', 'agent')
        agent_command = [spec.get('image', {}).get('agent', 'python3'), '-I', '-S', '-u',
                         '-c', agent_source(image=bool(spec.get('image'))), str(AGENT_PORT), token]
        command = agent_command
        if init in ('systemd', 'docker'):
            command = spec['template']['runtime_options'].get('init_command', ['/sbin/init'])
            if not isinstance(command, list) or not command or any(
                    not isinstance(arg, str) or '\0' in arg for arg in command):
                raise ValueError('init_command must be a nonempty list of arguments')
        archive = spec['template']['runtime_options'].get('docker_archive')
        if archive and snapshot is None:
            from ...workspace import assets, _immutable
            relative = Path(archive)
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('Docker archive must name a relative immutable asset')
            _immutable(assets() / relative, self.root / relative)
            options += ['--docker-data', '--docker-archive', str(self.root / relative)]
            command = ['/usr/local/bin/engine-docker', 'init']
        with measure('runtime_launch_seconds'):
            from . import proxy
            selected_proxy, proxy_options = proxy.bind(self.root, identity, spec['resources']['network'], snapshot,
                                                       spec.get('_proxy_assignment'))
            options += proxy_options
            if spec['resources']['memory'].get('disk') is not None:
                from .engine import disk_runtime
                options += disk_runtime(self.root, snapshot)
            if snapshot:
                manifest = json.loads((Path(snapshot) / 'snapshot-manifest.json').read_text())
                self.manager.load(snapshot, identity, command=command if manifest['kind'] == 'filesystem' else (),
                                  options=options, timeout=remaining(),
                                  experimental_gpu_live=spec.get('experimental_gpu_live', False))
                cold = manifest['kind'] == 'filesystem'
            else:
                self.manager.start(identity, command=command, options=options,
                                   timeout=remaining())
                cold = True
        with measure('runtime_agent_seconds'):
            metadata = self._start_agent(identity, token, init, cold, deadline, remaining, agent_command)
            metadata.update(proxy.configure(self.connections[identity], selected_proxy))
            return metadata

    def _start_agent(self, identity, token, init, cold, deadline, remaining, agent_command):
        if init in ('systemd', 'docker') and cold:
            while True:
                try:
                    self.manager._run([*self.manager._command(identity), 'exec', identity,
                                       'test', '-S', '/run/systemd/private'], timeout=10)
                    break
                except RuntimeError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('guest systemd control bus did not become ready: ' + identity)
                    time.sleep(.1)
            self.manager._run([*self.manager._command(identity), 'exec', identity,
                               'systemd-run', '--unit=sandweave-agent', '--collect',
                               *agent_command], timeout=30)
        port = self.manager.status(identity)['ports'][str(AGENT_PORT)]
        client = Connection('127.0.0.1', port, token, timeout=min(1, remaining()))
        while True:
            try:
                client.call('ping')
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise TimeoutError('guest command service did not become ready: ' + identity)
                time.sleep(.1)
        client.close()
        self.connections[identity] = Connection('127.0.0.1', port, token, timeout=30)
        return {'port': port, 'token': token}

    def agent(self, identity, metadata):
        if identity not in self.connections:
            port = self.manager.status(identity)['ports'][str(AGENT_PORT)]
            self.connections[identity] = Connection('127.0.0.1', port, metadata['token'], timeout=30)
        return self.connections[identity]

    def detach(self, identity):
        connection = self.connections.pop(identity, None)
        if connection:
            connection.close()

    def status(self, identity):
        status = self.manager.status(identity)
        status['gpus'] = []
        if status['status'] in ('running', 'paused') and status.get('gpu'):
            # The launch record contains the selected device, whereas the
            # public spec only contains a model constraint (or gpu=True).
            try:
                from ..gpu_information import describe
                status['gpus'] = [describe(self.manager._settings(identity)['gpu'])]
            except (OSError, ValueError, KeyError):
                # Inspection must remain possible if runtime files disappear
                # during cleanup. Unknown is distinct from no selected device.
                status['gpus'] = None
        return status

    def pause(self, identity):
        self.detach(identity)
        return self.manager.pause(identity)

    def resume(self, identity):
        return self.manager.resume(identity)

    def capture(self, identity, label, state, *, experimental_gpu_live=False):
        if any(m['snapshot'] != 'rebind' for m in self.manager._settings(identity).get('external_mounts', [])):
            raise UnsupportedFeature('external writable mount rejects capture; explicitly choose snapshot="rebind" for shared state')
        self.detach(identity)
        mode = {'memory': 'live', 'filesystem': 'filesystem', 'auto': 'auto'}.get(state)
        if experimental_gpu_live and state == 'memory':
            mode = 'experimental-gpu-live'
        if mode is None:
            raise ValueError('snapshot state must be filesystem, memory or auto')
        return self.manager.save(identity, label, mode=mode)

    def terminate(self, identity):
        self.detach(identity)
        return self.manager.stop(identity, discard=True)

    def discard(self, identity):
        """Remove only this stopped sandbox's writable bundle and network files."""
        from ...retention import remove
        control = importlib.import_module('environment_control')
        with control.acquire_lock(self.manager.local, identity):
            status = self.manager.status(identity)
            if status['status'] in ('running', 'paused', 'starting'):
                raise ResourceUnavailable('cannot discard a live sandbox')
            importlib.import_module('disk_memory').cleanup(self.manager._logs(identity))
            remove(self.manager._bundle(identity))
            remove(self.manager.local / 'gvisor/network' / identity)
            (self.root / 'sandboxes' / (identity + '.mounts.json')).unlink(missing_ok=True)

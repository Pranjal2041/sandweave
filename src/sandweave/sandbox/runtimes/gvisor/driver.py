"""Adapter around qualified engine operations, run only inside the private worker."""
import importlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import shutil
import subprocess
import sys
import time

from ...connection import Connection
from ...errors import ResourceUnavailable, UnsupportedFeature
from ...resources import memory_bytes, gpu_matches
from ...workspace import locked, atomic_json
from ...timings import measure
from .... import _unix_sockets

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
            if gpu_matches(requested, model):
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
            # These non-secret descriptors are read by guest applications,
            # including desktop users other than root. Repair cached drivers
            # too: the worker's umask may have made them owner/group-only.
            for name in ('driver.json', 'egl.json', 'vulkan.json'):
                (destination / name).chmod(0o644)
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
                   '--network-policy', resources['network']['mode'],
                   '--allowed-hosts', json.dumps(resources['network'].get('allowed_hosts', []))]
        storage = spec.get('storage', {'mode': 'memory'})
        options += ['--storage', storage['mode']]
        if storage.get('path'):
            options += ['--storage-path', storage['path']]
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
        profile = runtime.get('profile', False)
        if not isinstance(profile, bool):
            raise ValueError('profile must be a boolean')
        if profile:
            options.append('--profile')
        docker_data = runtime.get('docker_data', bool(runtime.get('docker_archive')))
        if not isinstance(docker_data, bool):
            raise ValueError('docker_data must be a boolean')
        if runtime.get('docker_archive') and not docker_data:
            raise ValueError('docker_archive requires docker_data=True')
        if docker_data:
            options.append('--docker-data')
        if runtime.get('nftables'):
            options.append('--nftables')
        if runtime.get('virtual_consoles'):
            options.append('--virtual-consoles')
        for feature in ('netlink_address_events', 'sysctl_reapply'):
            if runtime.get(feature):
                options.append('--' + feature.replace('_', '-'))
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

    def profile(self, identity):
        import tempfile
        # runsc prints informational messages on stdout, so keep its binary
        # profile in a separate, uniquely owned file in the worker workspace.
        with tempfile.TemporaryDirectory(prefix='.profile-', dir=self.root / 'runs') as directory:
            path = Path(directory) / 'heap.pprof'
            output = '/lab/' + str(path.relative_to(self.root))
            result = subprocess.run([*self.manager._command(identity, bind_resources=False),
                                     'profile', 'heap', '--output=' + output, identity],
                                    capture_output=True, timeout=30)
            if result.returncode:
                raise RuntimeError(result.stderr.decode(errors='replace')[-4000:])
            data = path.read_bytes()
            if not data.startswith(b'\x1f\x8b'):
                raise RuntimeError('runtime returned an invalid heap profile')
            return data

    def create(self, identity, spec, *, snapshot=None, token=None):
        token = token or secrets.token_hex(32)
        guest_tools = None
        deadline = spec.get('_startup_deadline', time.monotonic() + spec.get('startup_timeout', 300))
        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError('runtime startup deadline exceeded: ' + identity)
            return value
        options = self.options(spec)
        if spec.get('_service_network'):
            network_file = self.root / 'sandboxes' / (identity + '.network.json')
            atomic_json(network_file, spec['_service_network'])
            options += ['--service-network', str(network_file)]
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
            options += ['--docker-archive', str(self.root / relative)]
            command = ['/usr/local/bin/engine-docker', 'init']
        with measure('runtime_launch_seconds'):
            from . import proxy
            selected_proxy, proxy_options = proxy.bind(self.root, identity, spec['resources']['network'], snapshot,
                                                       spec.get('_proxy_assignment'))
            options += proxy_options
            features = ['disk-storage'] if spec.get('storage', {}).get('mode') == 'disk' else []
            if spec.get('_guest_tools') or any(mount.get('_private_volume') for mount in spec.get('mounts', [])):
                features.append('private-volumes')
            if any(mount.get('_private_volume') for mount in spec.get('mounts', [])):
                features.append('private-volume-devices')
            if any(mount.get('_exclusive') for mount in spec.get('mounts', [])):
                features.append('private-volume-cache')
            if spec['resources']['memory'].get('disk') is not None:
                features.append('app-memory-directory')
            if spec['template'].get('runtime_options', {}).get('virtual_consoles'):
                features.append('virtual-consoles')
            for feature in ('netlink_address_events', 'sysctl_reapply'):
                if spec['template'].get('runtime_options', {}).get(feature):
                    features.append(feature.replace('_', '-'))
            if features:
                from .engine import feature_runtime
                selected_runtime = feature_runtime(self.root, features, snapshot)
                options += selected_runtime
                if 'private-volumes' in features and selected_runtime:
                    guest_tools = self.root / selected_runtime[1] / 'gvisor-bin/sandweave-guest-tools'
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
            if guest_tools is not None:
                prefix = '/.sandweave-runtime' if spec.get('image') else '/var/lib/sandweave'
                self.connections[identity].call('file', op='write', path=prefix + '/tools/guest-tools',
                    data=guest_tools.read_bytes(), truncate=True, mode=0o700)
            metadata.update(proxy.configure(self.connections[identity], selected_proxy))
            return metadata

    def _start_agent(self, identity, token, init, cold, deadline, remaining, agent_command):
        if init in ('systemd', 'docker') and cold:
            while True:
                try:
                    self.manager._run([*self.manager._command(identity), 'exec', identity,
                                       'test', '-S', '/run/systemd/private'], timeout=min(10, remaining()))
                    break
                except (RuntimeError, subprocess.TimeoutExpired):
                    if time.monotonic() >= deadline:
                        raise TimeoutError('guest systemd control bus did not become ready: ' + identity)
                    time.sleep(.1)
            self.manager._run([*self.manager._command(identity), 'exec', identity,
                               'systemd-run', '--unit=sandweave-agent', '--collect', '--no-block',
                               *agent_command], timeout=remaining())
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

    def network_policy(self, identity, network):
        local = Path((self.root / 'runs/local-path.txt').read_text().strip())
        endpoint = local / 'gvisor/network' / identity / 'ethernet.sock.control'
        update = {'mode': network['mode'], 'allow_cidrs': network.get('allow_cidrs', []),
                  'proxy_endpoints': [], 'allowed_hosts': network.get('allowed_hosts', [])}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(10)
            _unix_sockets.connect(channel, endpoint)
            channel.sendall(json.dumps(update).encode() + b'\n')
            with channel.makefile('rb') as stream:
                result = json.loads(stream.readline(65537))
            if not result.get('ok'):
                raise RuntimeError(result.get('error', 'network policy update failed'))
        bundle = local / 'gvisor/bundles' / identity
        config = json.loads((bundle / 'network-policy.json').read_text())
        atomic_json(bundle / 'network-policy.json', {**config, **update})
        settings = json.loads((bundle / 'launch-settings.json').read_text())
        settings['settings']['network_policy'] = network['mode']
        settings['settings']['allow_cidr'] = network.get('allow_cidrs', [])
        settings['settings']['allowed_hosts'] = network.get('allowed_hosts', [])
        atomic_json(bundle / 'launch-settings.json', settings)

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
            importlib.import_module('filesystem_storage').cleanup(self.manager._logs(identity))
            remove(self.manager._bundle(identity))
            remove(self.manager.local / 'gvisor/network' / identity)
            (self.root / 'sandboxes' / (identity + '.mounts.json')).unlink(missing_ok=True)

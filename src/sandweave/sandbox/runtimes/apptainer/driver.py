"""Native Apptainer command environments; state and enforcement limits are explicit."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import stat
import subprocess
import threading
import time
import uuid

from ...connection import Connection
from ...errors import UnsupportedFeature, ResourceUnavailable
from ...resources import memory_bytes
from ...workspace import assets, atomic_json, locked, _immutable
from ..gvisor.driver import agent_source


class Runtime:
    def __init__(self, root, gvisor):
        self.root, self.gvisor = Path(root), gvisor
        self.local = Path((self.root / 'runs/local-path.txt').read_text().strip()) / 'native'
        self.local.mkdir(exist_ok=True)
        self.connections, self.children = {}, {}
        import cpu_broker
        self.ownership = cpu_broker

    @staticmethod
    def descriptor():
        return {'name': 'apptainer', 'state': ['filesystem'], 'guest_privilege': 'single root-mapped UID',
                'cpu': 'affinity only; virtual CPU advertisement, weights and quotas unavailable',
                'memory': 'sampled aggregate RSS guard; no virtual-kernel page allocator',
                'network': 'host network; gVisor egress filtering unavailable',
                'isolation': 'native host kernel with user, mount, PID and IPC namespaces'}

    def directory(self, identity):
        return self.local / identity

    def metadata(self, identity):
        return json.loads((self.directory(identity) / 'native.json').read_text())

    def create(self, identity, spec, *, snapshot=None, token=None):
        resources = spec['resources']
        if spec['template']['capabilities'] or spec['template']['runtime_options'].get('init', 'agent') != 'agent':
            raise UnsupportedFeature('this native adapter provides command templates; desktop/systemd controls require gVisor')
        if resources['cpu']['weight'] != 100 or resources['cpu']['quota'] is not None:
            raise UnsupportedFeature('native CPU weights and quotas are unavailable; this adapter uses affinity')
        if resources['network']['mode'] != 'internet' or resources['network']['allow_cidrs']:
            raise UnsupportedFeature('native egress filtering is unavailable; the explicit native runtime shares host networking')
        if resources['gpu'] and (resources['gpu'].get('sm_chunks') or resources['gpu'].get('client_memory')):
            raise UnsupportedFeature('native MPS partition management is not registered')
        cpus = sorted(os.sched_getaffinity(0))
        if resources['cpu']['vcpus'] > len(cpus):
            raise ResourceUnavailable('native CPU affinity request exceeds the allocated CPU set')
        cpus = cpus[:resources['cpu']['vcpus']]
        directory = self.directory(identity)
        directory.mkdir(mode=0o700)
        for name in ('overlay/upper', 'overlay/work', 'control'):
            (directory / name).mkdir(parents=True)
        image = self.root / 'tools/native-base.sif'
        with locked(self.root / 'tools/.native-base.lock'):
            _immutable(assets() / 'tools/gvisor-builder.sif', image)
        if snapshot:
            manifest = json.loads((Path(snapshot) / 'snapshot-manifest.json').read_text())
            if manifest.get('backend') != 'apptainer' or manifest['kind'] != 'filesystem':
                raise UnsupportedFeature('native restore requires a native filesystem snapshot')
            shutil.copytree(Path(snapshot) / 'upper', directory / 'overlay/upper', dirs_exist_ok=True, symlinks=True)
        token = token or secrets.token_hex(32)
        source = directory / 'control/agent.py'
        source.write_text(agent_source())
        binds = []
        for mount in spec.get('mounts', []):
            binds += ['--bind', mount['source'] + ':' + mount['destination'] + (':ro' if mount['read_only'] else ':rw')]
        if resources['gpu']:
            selected = self.gvisor.gpu(resources['gpu'])
            import gvisor_gpu
            for device in gvisor_gpu.allocated_device(selected):
                binds += ['--bind', device['path'] + ':' + device['path']]
            binds += ['--bind', str(self.root / 'tools/gpu') + ':/opt/engine-gpu:ro']
            for script in ('gvisor-guest-gpu.sh', 'gvisor-guest-gpu-init.sh'):
                shutil.copy2(self.root / 'scripts' / script, directory / 'control' / script)
        command = ['apptainer', 'exec', '--userns', '--fakeroot', '--containall', '--cleanenv', '--no-home',
                   '--overlay', str(directory / 'overlay'), '--bind', str(directory / 'control') + ':/sdk',
                   *binds, str(image)]
        if resources['gpu']:
            command += ['sh', '/sdk/gvisor-guest-gpu-init.sh', 'sh', '/sdk/gvisor-guest-gpu.sh']
        command += ['python3', '-u', '/sdk/agent.py', '/sdk/agent.sock', token]
        # taskset applies eligibility before any native descendants are spawned.
        command = ['taskset', '-c', ','.join(map(str, cpus)), *command]
        with (directory / 'launcher.log').open('ab') as log:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        self.children[identity] = process
        record = {'id': identity, 'pid': process.pid, 'start': self.ownership.process_table([process.pid])[process.pid]['start'],
                  'state': 'running', 'gpu': bool(resources['gpu']), 'cpus': cpus,
                  'memory_limit': memory_bytes(resources['memory']['guest']) + memory_bytes(resources['memory']['runtime']),
                  'mounts': spec.get('mounts', [])}
        atomic_json(directory / 'native.json', record)
        client = Connection('localhost', 0, token, unix_path=str(directory / 'control/agent.sock'), timeout=30)
        deadline = spec.get('_startup_deadline', time.monotonic() + spec.get('startup_timeout', 300))
        while True:
            if process.poll() is not None:
                raise ResourceUnavailable('native guest failed: ' + (directory / 'launcher.log').read_text()[-4000:])
            try:
                client.call('ping')
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise TimeoutError('native command agent did not become ready')
                time.sleep(.05)
        self.connections[identity] = client
        threading.Thread(target=self._memory_guard, args=(identity,), daemon=True).start()
        return {'socket': str(directory / 'control/agent.sock'), 'token': token}

    def _members(self, record):
        info = self.ownership.process_table([record['pid']]).get(record['pid'])
        if not info or info['start'] != record['start'] or info['state'] == 'Z':
            return {}
        return self.ownership.discover_trees([record['pid']])

    def _memory_guard(self, identity):
        while True:
            record = self.metadata(identity)
            members = self._members(record)
            if not members:
                return
            rss = 0
            for pid in members:
                try:
                    for line in Path(f'/proc/{pid}/status').read_text().splitlines():
                        if line.startswith('VmRSS:'):
                            rss += int(line.split()[1]) * 1024
                except FileNotFoundError:
                    pass
            if rss > record['memory_limit']:
                self.terminate(identity)
                record.update(state='stopped', error='native aggregate RSS limit exceeded', observed_rss=rss)
                atomic_json(self.directory(identity) / 'native.json', record)
                return
            time.sleep(.2)

    def status(self, identity):
        if not (self.directory(identity) / 'native.json').exists():
            return {'status': 'missing', 'name': identity}
        record = self.metadata(identity)
        running = bool(self._members(record))
        return {'name': identity, 'status': record['state'] if running else 'stopped', 'gpu': record['gpu'],
                'logs': str(self.directory(identity)), 'runtime': self.descriptor(), 'error': record.get('error')}

    def agent(self, identity, metadata):
        if identity not in self.connections:
            self.connections[identity] = Connection('localhost', 0, metadata['token'],
                unix_path=metadata['socket'], timeout=30)
        return self.connections[identity]

    def _detach(self, identity):
        client = self.connections.pop(identity, None)
        if client:
            client.close()

    def _signal(self, members, signum):
        # Use the qualified pidfd helper, checking start times before signalling.
        for pid, info in members.items():
            self.gvisor.manager._signal(pid, info['start'], signum)

    def pause(self, identity):
        record = self.metadata(identity)
        if record['state'] == 'paused':
            return self.status(identity)
        self._detach(identity)
        stopped = {}
        for _ in range(3):
            members = self._members(record)
            self._signal(members, signal.SIGSTOP)
            stopped.update(members)
        record.update(state='paused', paused_members={str(k): v for k, v in stopped.items()})
        atomic_json(self.directory(identity) / 'native.json', record)
        return self.status(identity)

    def resume(self, identity):
        record = self.metadata(identity)
        self._signal({int(k): v for k, v in record.pop('paused_members', {}).items()}, signal.SIGCONT)
        record['state'] = 'running'
        atomic_json(self.directory(identity) / 'native.json', record)
        return self.status(identity)

    def terminate(self, identity):
        if not (self.directory(identity) / 'native.json').exists():
            return self.status(identity)
        self._detach(identity)
        record = self.metadata(identity)
        members = self._members(record)
        self._signal(members, signal.SIGCONT)
        self._signal(members, signal.SIGTERM)
        deadline = time.monotonic() + 3
        while self._members(record) and time.monotonic() < deadline:
            time.sleep(.05)
        self._signal(self._members(record), signal.SIGKILL)
        process = self.children.pop(identity, None)
        if process:
            process.wait(timeout=5)
        record['state'] = 'stopped'
        atomic_json(self.directory(identity) / 'native.json', record)
        return self.status(identity)

    def capture(self, identity, label, state, *, experimental_gpu_live=False):
        if any(m['snapshot'] != 'rebind' for m in self.metadata(identity).get('mounts', [])):
            raise UnsupportedFeature('external writable mount rejects capture; explicitly choose snapshot="rebind" for shared state')
        if state not in ('filesystem', 'auto'):
            raise UnsupportedFeature('native memory checkpoints are unavailable; choose filesystem state')
        previous = self.status(identity)['status']
        if previous not in ('running', 'paused'):
            raise ValueError('native capture requires a running or paused sandbox')
        self.pause(identity)
        destination = self.root / 'snapshots' / label
        started = time.monotonic()
        try:
            upper = self.directory(identity) / 'overlay/upper'
            for path in upper.rglob('*'):
                if not (path.is_file() or path.is_dir() or path.is_symlink()):
                    raise UnsupportedFeature('native snapshot cannot copy special overlay entry: ' + str(path.relative_to(upper)))
            destination.mkdir(mode=0o700)
            shutil.copytree(upper, destination / 'upper', symlinks=True)
            image = self.root / 'tools/native-base.sif'
            manifest = {'format': 1, 'backend': 'apptainer', 'kind': 'filesystem', 'snapshot_id': uuid.uuid4().hex,
                        'runtime': {'name': 'apptainer', 'version': subprocess.check_output(['apptainer', '--version'], text=True).strip()},
                        'base_image': {'path': 'tools/native-base.sif', 'size': image.stat().st_size, 'sha256': digest(image)},
                        'files': inventory(destination / 'upper')}
            atomic_json(destination / 'snapshot-manifest.json', manifest)
            atomic_json(destination / 'verification.json', {'status': 'passed', 'snapshot_id': manifest['snapshot_id']})
            return {'snapshot': str(destination), 'kind': 'filesystem', 'timings': {'total_seconds': time.monotonic()-started}}
        finally:
            if previous == 'running':
                self.resume(identity)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inventory(root):
    result = {}
    for path in sorted(root.rglob('*')):
        info = path.lstat()
        entry = {'mode': stat.S_IMODE(info.st_mode)}
        if path.is_symlink():
            entry.update(kind='symlink', target=os.readlink(path))
        elif path.is_file():
            entry.update(kind='file', size=info.st_size, sha256=digest(path))
        elif path.is_dir():
            entry.update(kind='directory')
        else:
            raise UnsupportedFeature('native snapshot contains an unsupported special entry')
        entry['xattrs'] = {name: os.getxattr(path, name, follow_symlinks=False).hex()
                          for name in os.listxattr(path, follow_symlinks=False)}
        result[str(path.relative_to(root))] = entry
    return result


def verify(workspace, snapshot):
    manifest = json.loads((snapshot / 'snapshot-manifest.json').read_text())
    base = manifest['base_image']
    relative = Path(base['path'])
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('native snapshot dependency escapes its workspace')
    image = workspace / relative
    valid = (manifest.get('backend') == 'apptainer' and image.stat().st_size == base['size']
             and digest(image) == base['sha256'] and inventory(snapshot / 'upper') == manifest['files'])
    result = {'status': 'passed' if valid else 'failed', 'snapshot_id': manifest['snapshot_id']}
    atomic_json(snapshot / 'verification.json', result)
    return result

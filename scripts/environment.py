"""Python lifecycle API for the standalone no-KVM lab."""
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import signal
import platform
import socket
import subprocess
import sys
import time
import uuid

import cpu_broker
import environment_control as control
import runtime_store
import snapshot_store
import fast_io
import disk_memory


def runtime_state(path):
    """Read runsc's state under its lock; a busy writer is still starting."""
    try:
        with path.with_suffix('.lock').open('rb') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return None
            return json.loads(path.read_text())
    except FileNotFoundError:
        return None


class EnvironmentManager:
    def __init__(self, lab=None):
        self.lab = Path(lab or Path(__file__).resolve().parent.parent).resolve()
        self.local = Path((self.lab / 'runs/local-path.txt').read_text().strip())

    def _logs(self, name):
        return self.lab / 'runs/gvisor' / control.valid_name(name)

    def _bundle(self, name):
        return self.local / 'gvisor/bundles' / control.valid_name(name)

    def _settings(self, name):
        return json.loads((self._bundle(name) / 'launch-settings.json').read_text())

    def _launcher(self, name):
        path = self._logs(name) / 'launcher.json'
        if path.exists():
            record = json.loads(path.read_text())
            if record['hostname'] != socket.gethostname():
                return None
            pid = record['pid']
        else:
            try:
                pid = int((self._logs(name) / 'launcher-pid.txt').read_text())
            except FileNotFoundError:
                return None
            record = None
        info = cpu_broker.process_table([pid]).get(pid)
        if info is None or info['state'] == 'Z' or (record and info['start'] != record['start']):
            return None
        try:
            argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        except FileNotFoundError:
            return None
        if str(self.lab / 'scripts/run-gvisor.py').encode() not in argv or name.encode() not in argv:
            return None
        return {'pid': pid, 'start': info['start']}

    def status(self, name):
        """Return runtime state; running means the kernel, not application readiness."""
        bundle = self._bundle(name)
        launcher = self._launcher(name)
        result = {'name': name, 'status': 'starting' if launcher else 'stopped',
                  'launcher': launcher, 'logs': str(self._logs(name))}
        path = self.local / 'gvisor/state' / f'{name}_sandbox:{name}.state'
        state = runtime_state(path)
        if state and state.get('sandbox', {}).get('pid'):
            pid = state['sandbox']['pid']
            info = cpu_broker.process_table([pid]).get(pid)
            if info and info['state'] != 'Z':
                settings = self._settings(name)
                runtime = runtime_store.validate(self.lab, settings['runtime'], verify=False)
                try:
                    matches = os.path.samefile(f'/proc/{pid}/exe', runtime / 'gvisor-bin/gvisor_sentry')
                    argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
                except FileNotFoundError:
                    matches, argv = False, []
                if not matches or f'--bundle=/local/gvisor/bundles/{name}'.encode() not in argv:
                    raise RuntimeError(f'{name}: saved runtime PID does not identify this environment')
                result.update(status=state['status'], sentry={'pid': pid, 'start': info['start']},
                              gpu=bool(settings.get('gpu')))
        if not bundle.exists() and launcher is None:
            result['status'] = 'missing'
        for filename, key in [('ports.json', 'ports'), ('stopped.json', 'last_stop'),
                              ('disk-memory.json', 'disk_memory'), ('proxy.json', 'proxy')]:
            try:
                result[key] = json.loads((self._logs(name) / filename).read_text())
            except FileNotFoundError:
                pass
        return result

    def list(self, *, active_only=False):
        names = {p.name for p in (self.local / 'gvisor/bundles').iterdir() if p.is_dir()}
        result = [self.status(name) for name in sorted(names)]
        return [s for s in result if s['status'] not in ('missing', 'stopped')] if active_only else result

    def _command(self, name):
        settings = self._settings(name)
        runtime = runtime_store.validate(self.lab, settings['runtime'], verify=False)
        command = [str(self.lab / 'scripts/gvisor-host.sh')]
        if settings.get('external_mounts'):
            command += ['--mounts', str(self._bundle(name) / 'external-mounts.json')]
        if settings.get('gpu'):
            command += ['--gpu', str(settings['gpu']['device_minor'])]
        return command + ['/lab/' + str(runtime.relative_to(self.lab)) + '/runsc', '--root=/local/gvisor/state']

    def _run(self, command, *, timeout=30, **kwargs):
        result = subprocess.run(command, cwd=self.lab, capture_output=True, text=True, timeout=timeout, **kwargs)
        if result.returncode:
            raise RuntimeError((result.stderr + result.stdout)[-6000:].strip())
        return result.stdout

    def _pause(self, name):
        state = self.status(name)
        if state['status'] not in ('running', 'paused'):
            raise ValueError(f"cannot pause {name} in state {state['status']}")
        created = control.release_cpu(self.local, name)
        try:
            fast_io.detach(self, name)
            if state['status'] == 'running':
                self._run([*self._command(name), 'pause', name])
        except BaseException:
            for marker in created:
                marker.unlink(missing_ok=True)
            raise
        return self.status(name)

    def pause(self, name):
        with control.acquire_lock(self.local, name):
            return self._pause(name)

    def _resume(self, name):
        state = self.status(name)
        if state['status'] not in ('running', 'paused'):
            raise ValueError(f"cannot resume {name} in state {state['status']}; load a snapshot after stopping")
        if state['status'] == 'paused':
            self._run([*self._command(name), 'resume', name])
        control.resume_cpu(self.local, name)
        return self.status(name)

    def resume(self, name):
        with control.acquire_lock(self.local, name):
            return self._resume(name)

    def _save(self, name, label, mode, local_only, lock):
        control.valid_name(label)
        if mode not in ('auto', 'live', 'filesystem', 'experimental-gpu-live'):
            raise ValueError('unknown snapshot mode: ' + mode)
        state = self.status(name)
        if state['status'] not in ('running', 'paused'):
            raise ValueError(f"cannot save {name} in state {state['status']}")
        if mode == 'auto':
            mode = 'filesystem' if state['gpu'] else 'live'
        if state['gpu'] and mode == 'live':
            raise ValueError('GPU live save requires mode="experimental-gpu-live"')
        command = [sys.executable, str(self.lab / 'scripts/checkpoint-gvisor.py')]
        if mode in ('filesystem', 'experimental-gpu-live'):
            command += ['--' + mode]
        if local_only:
            command += ['--local-only']
        self._run([*command, name, label], timeout=None, pass_fds=(lock.fileno(),),
                  env={**os.environ, control.LOCK_FD_ENV: str(lock.fileno())})
        snapshot = (self.local / 'gvisor/checkpoints' if local_only else self.lab / 'snapshots') / label
        manifest = snapshot_store.inspect(self.lab, snapshot)
        return {'snapshot': str(snapshot), 'kind': manifest.get('kind', 'live'),
                'experimental_gpu_live': manifest.get('experimental_gpu_live', False),
                'timings': json.loads((snapshot / 'save-timings.json').read_text()),
                'verification': str(snapshot / 'verification.json')}

    def save(self, name, label, *, mode='auto', local_only=False):
        """Save without changing the source's running/paused state."""
        with control.acquire_lock(self.local, name) as lock:
            return self._save(name, label, mode, local_only, lock)

    def _launch(self, name, command, options, timeout):
        control.valid_name(name)
        with control.acquire_lock(self.local, name):
            if self._bundle(name).exists():
                raise ValueError('environment name already exists; use a fresh name')
            self._run([sys.executable, str(self.lab / 'scripts/run-gvisor.py'), '--detach',
                       *options, name, '--', *command], timeout=30)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                state = self.status(name)
                if state['status'] == 'running':
                    # Filesystem restore becomes running before its mounts are installed.
                    pending_fs = False
                    if '--restore' in options:
                        saved = Path(options[options.index('--restore') + 1])
                        manifest = json.loads((saved / 'snapshot-manifest.json').read_text())
                        pending_fs = manifest.get('kind') == 'filesystem' and not (self._logs(name) / 'filesystem-ready.json').exists()
                    if not pending_fs:
                        return state
                if state['launcher'] is None:
                    output = (self._logs(name) / 'launcher.out').read_text(errors='replace')
                    raise RuntimeError(f'{name}: launcher exited before readiness\n' + output[-6000:])
                time.sleep(.1)
            raise TimeoutError(f'{name}: startup is still in progress; inspect {self._logs(name)} or stop it explicitly')

    def start(self, name, *, command=('/sbin/init',), options=(), timeout=120):
        return self._launch(name, list(command), list(options), timeout)

    def load(self, snapshot, name, *, experimental_gpu_live=False, verify=False, command=(), options=(), timeout=300):
        snapshot = Path(snapshot).resolve()
        manifest = snapshot_store.inspect(self.lab, snapshot)
        settings = json.loads((snapshot / 'launch-settings.json').read_text())
        if command and manifest.get('kind', 'live') != 'filesystem':
            raise ValueError('command overrides require a filesystem snapshot; live restore resumes saved processes')
        if manifest.get('kind', 'live') == 'live' and settings.get('gpu') and not experimental_gpu_live:
            raise ValueError('live GPU restore requires experimental_gpu_live=True')
        flags = ['--restore', str(snapshot), *options]
        if experimental_gpu_live:
            flags += ['--experimental-gpu-live']
        if verify:
            flags += ['--verify']
        return self._launch(name, list(command), flags, timeout)

    restore = load

    def fast_io(self, name, *, backend='xvnc'):
        return fast_io.FastIOClient(name, manager=self, backend=backend)

    def stop(self, name, *, discard=False, label=None, mode='auto', timeout=30):
        """Save durably before terminating; discard=True explicitly skips the save."""
        if discard and (label is not None or mode != 'auto'):
            raise ValueError('discard cannot be combined with snapshot options')
        with control.acquire_lock(self.local, name) as lock:
            state = self.status(name)
            if state['status'] == 'missing':
                raise ValueError('unknown environment: ' + name)
            if state['status'] == 'stopped':
                disk_memory.cleanup(self._logs(name))
                return state
            if state['status'] not in ('running', 'paused') and not discard:
                raise ValueError('environment is still starting; wait for readiness or explicitly discard it')
            saved = None
            if discard:
                fast_io.detach(self, name, stop=True, discard=True)
            if not discard:
                label = label or f'{name}-stop-{uuid.uuid4().hex[:12]}'
                # Hold the source at the saved cut until it has been stopped.
                self._pause(name)
                try:
                    saved = self._save(name, label, mode, False, lock)
                except BaseException:
                    if state['status'] == 'running':
                        self._resume(name)
                    raise
                fast_io.detach(self, name, stop=True)
            snapshot_store.write_json(self._logs(name) / 'stopped.json', {
                'requested_at': time.time(), 'discarded': discard, 'saved': saved, 'complete': False})
            launcher = self._launcher(name)
            members = self._owned_members(name, launcher, state.get('sentry'))
            if state.get('sentry'):
                self._run([*self._command(name), 'delete', '--force', name], timeout=timeout)
            elif launcher:
                self._signal(launcher['pid'], launcher['start'], signal.SIGTERM)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                remaining = self._remaining(members)
                if not remaining:
                    break
                time.sleep(.1)
            else:
                for pid, info in remaining.items():
                    self._signal(pid, info['start'], signal.SIGKILL)
                time.sleep(.2)
                if self._remaining(members):
                    raise TimeoutError('owned runtime processes did not stop; snapshot is retained')
            control.resume_cpu(self.local, name)
            record = {'requested_at': time.time(), 'discarded': discard, 'saved': saved, 'complete': True}
            snapshot_store.write_json(self._logs(name) / 'stopped.json', record)
            disk_memory.cleanup(self._logs(name))
            return self.status(name)

    @staticmethod
    def _remaining(members):
        current = cpu_broker.process_table(members)
        return {p: s for p, s in current.items() if s['start'] == members[p]['start'] and s['state'] != 'Z'}

    def _owned_members(self, name, launcher, sentry):
        roots = [sentry['pid']] if sentry else []
        if launcher:
            path = self._logs(name) / 'owned-processes.json'
            if path.exists():
                owned = json.loads(path.read_text())
                if owned['launcher'] != launcher['pid']:
                    raise RuntimeError('helper ownership does not match the launcher')
                for entry in owned['processes']:
                    info = cpu_broker.process_table([entry['pid']]).get(entry['pid'])
                    if info and info['start'] == entry['start']:
                        roots.append(entry['pid'])
            else:
                # Legacy launchers lack a ledger. Select their direct transport
                # and Apptainer children, never shared CPU/MPS services.
                for task in Path(f"/proc/{launcher['pid']}/task").glob('*/children'):
                    for pid in map(int, task.read_text().split()):
                        try:
                            argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
                        except FileNotFoundError:
                            continue
                        relay = str(self.lab / 'scripts/ethernet-relay.py').encode()
                        if argv[0] == b'passt' or argv[0].startswith(b'Apptainer runtime parent:') or relay in argv:
                            roots.append(pid)
        members = cpu_broker.discover_trees(roots)
        if launcher:
            info = cpu_broker.process_table([launcher['pid']]).get(launcher['pid'])
            if info and info['start'] == launcher['start']:
                members[launcher['pid']] = info
        return members

    @staticmethod
    def _signal(pid, start, signum):
        # The cluster's Conda Python/libc lack the pidfd wrappers. These Linux
        # syscall numbers are shared by the two architectures supported here.
        if platform.machine() not in ('x86_64', 'aarch64'):
            raise RuntimeError('pidfd signalling requires x86_64 or aarch64 Linux')
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        fd = libc.syscall(434, int(pid), 0)  # pidfd_open
        if fd < 0:
            code = ctypes.get_errno()
            if code != errno.ESRCH:
                raise OSError(code, os.strerror(code))
            return
        try:
            info = cpu_broker.process_table([pid]).get(pid)
            if info and info['start'] == start:
                if libc.syscall(424, int(fd), int(signum), 0, 0) < 0:  # pidfd_send_signal
                    code = ctypes.get_errno()
                    if code != errno.ESRCH:
                        raise OSError(code, os.strerror(code))
        finally:
            os.close(fd)

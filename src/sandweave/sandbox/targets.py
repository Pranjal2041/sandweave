"""Explicit worker placement. A client connection never owns unrelated jobs."""
import json
import asyncio
import argparse
import atexit
from dataclasses import dataclass
import os
from pathlib import Path
import socket
import shlex
import subprocess
import sys
import time
import threading
import uuid

from .connection import Connection
from .errors import ResourceUnavailable
from .workspace import home, locked, atomic_json, worker_key, assets, asset_identity, software_identity
from .asyncio import dualmethod, dualclassmethod
from .resources import memory_bytes, positive

_tunnels, _tunnel_lock = {}, threading.Lock()


def _validate_host(host):
    if not isinstance(host, str) or not host or host.startswith('-') or any(c.isspace() for c in host):
        raise ValueError('SSH host must be a host alias or user@hostname')
    return host


def _ssh(host, command, *, timeout=180, port=None):
    _validate_host(host)
    return subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                           *(['-p', str(port)] if port is not None else []), host,
                           shlex.join(command)], capture_output=True, text=True, timeout=timeout, check=True).stdout


def _tunnel(host, port, *, ssh_port=None):
    _validate_host(host)
    key = host, port, ssh_port
    with _tunnel_lock:
        previous = _tunnels.get(key)
        if previous and previous[0].poll() is None:
            return previous[1]
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            local_port = reservation.getsockname()[1]
        process = subprocess.Popen(['ssh', '-N', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
            '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
            *(['-p', str(ssh_port)] if ssh_port is not None else []),
            '-L', f'127.0.0.1:{local_port}:127.0.0.1:{port}', host], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        deadline = time.monotonic() + 15
        while True:
            if process.poll() is not None:
                raise ResourceUnavailable('SSH tunnel failed: ' + process.stderr.read().decode(errors='replace')[-2000:])
            try:
                with socket.create_connection(('127.0.0.1', local_port), timeout=.2):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    process.terminate(); process.wait(timeout=5)
                    raise ResourceUnavailable('SSH tunnel did not become ready')
                time.sleep(.05)
        _tunnels[key] = process, local_port
        return local_port


@atexit.register
def _close_owned_tunnels():
    for process, port in _tunnels.values():
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()
        if process.stderr:
            process.stderr.close()


def _worker_environment(config):
    environment = {}
    for name, key in (('SANDWEAVE_HOME', 'home'), ('SANDWEAVE_ASSETS', 'assets'), ('PYTHONPATH', 'pythonpath')):
        value = config.get(key) or os.environ.get(name)
        if value:
            environment[name] = str(value)
    return environment


def _slurm_environment(config):
    environment = _worker_environment(config)
    # Slurm placement uses shared storage. Pin the effective configured paths,
    # including a location selected by setup rather than an environment variable.
    environment.setdefault('SANDWEAVE_HOME', str(home()))
    environment['SANDWEAVE_ASSETS'] = str(assets(directory=config.get('home'), selected=config.get('assets')))
    return environment


def local_connection(*, template=None):
    from .preparation import Installation, ensure
    installation = ensure(template) if template is not None else Installation(home(), assets())
    key = worker_key(installation.assets)
    directory = installation.directory / 'connections' / key
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = directory / 'worker.json'
    with locked(directory / 'startup.lock'):
        _wait_stopping(metadata)
        if metadata.exists():
            info = json.loads(metadata.read_text())
            connection = Connection('127.0.0.1', info['port'], info['token'], timeout=2)
            try:
                connection.call('ping')
                connection.close()
                return Connection('127.0.0.1', info['port'], info['token'])
            except Exception:
                connection.close()
                # A failed health check is not permission to replace a live
                # authority. Distinguish a dead PID from an unavailable worker.
                try:
                    command = Path(f'/proc/{info["pid"]}/cmdline').read_bytes().split(b'\0')
                except FileNotFoundError:
                    command = []
                if b'sandweave.sandbox.worker' in command and str(metadata).encode() in command:
                    raise ResourceUnavailable('existing worker is alive but unreachable; inspect ' + str(directory / 'worker.log'))
                metadata.unlink()
        from .ownership import process_alive, process_scope, process_state
        from ..setup_progress import Stage
        launcher = directory / 'launcher.json'
        saved = json.loads(launcher.read_text()) if launcher.is_file() else None
        child = None
        alive = process_alive(saved) if saved else False
        if alive is None:
            raise ResourceUnavailable('cannot establish the preparing worker process identity; inspect ' + str(launcher))
        def start():
            with (directory / 'worker.log').open('ab') as log:
                environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2]) +
                               os.pathsep + os.environ.get('PYTHONPATH', '')}
                environment.update(SANDWEAVE_HOME=str(installation.directory),
                                   SANDWEAVE_ASSETS=str(installation.assets))
                child = subprocess.Popen([sys.executable, '-m', 'sandweave.sandbox.worker',
                                          '--metadata', str(metadata)], stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                         env=environment)
            try:
                _, started = process_state(child.pid)
            except FileNotFoundError:
                raise ResourceUnavailable('worker exited during preparation; inspect ' + str(directory / 'worker.log')) from None
            saved = {'pid': child.pid, 'started': started, 'scope': process_scope()}
            atomic_json(launcher, saved)
            return child, saved
        if not alive:
            child, saved = start()
        # Preparing immutable files can outlast a sandbox startup deadline on
        # network storage. Wait for this same worker, retaining its identity
        # across client interruption so retry cannot launch a duplicate.
        with Stage('Preparing worker files', detail='Checking and staging runtime files',
                   log=directory / 'worker.log') as progress:
            while not metadata.exists():
                alive = process_alive(saved)
                if child is None and alive is False:
                    # A previous worker can remove its endpoint just before it
                    # exits. Wait for that identity to die, then start once;
                    # do not mistake its final shutdown for a failed new launch.
                    child, saved = start()
                    continue
                if (child is not None and child.poll() is not None) or alive is False:
                    raise ResourceUnavailable('worker failed to start: ' + (directory / 'worker.log').read_text()[-5000:])
                progress.update()
                time.sleep(.1)
        info = json.loads(metadata.read_text())
        connection = Connection('127.0.0.1', info['port'], info['token'])
        connection.call('ping')
        return connection


def connect(target=None, *, template=None):
    if target in (None, 'local'):
        return local_connection(template=template)
    from ..weave.client import ClusterConnection, cluster_config
    cluster = cluster_config(target)
    if cluster is not None:
        return ClusterConnection(cluster)
    if isinstance(target, (Slurm, Endpoint)):
        return target.connection()
    if isinstance(target, str) and target.startswith('ssh://'):
        target = {'host': target.removeprefix('ssh://')}
    if isinstance(target, str):
        config_path = home() / 'config.json'
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
        try:
            target = config['targets'][target]
        except KeyError:
            raise ResourceUnavailable('target is not configured: ' + str(target)) from None
    if not isinstance(target, dict):
        raise ValueError('target must be local, an SSH URI, configured name, mapping or Slurm allocation')
    if 'job_id' in target:
        return Slurm.connect(target['job_id'], **{k: v for k, v in target.items() if k != 'job_id'}).connection()
    host = target['host']
    if target.get('metadata'):
        command = [target.get('python', 'python3'), '-c',
                   'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))))', str(target['metadata'])]
    else:
        command = ['env', *[f'{k}={v}' for k, v in _worker_environment(target).items()],
                   target.get('python', 'python3'), '-m', 'sandweave.sandbox.targets', '--ensure']
        if template is not None:
            from ..onboarding import workload
            command += ['--template', workload(template)]
    info = json.loads(_ssh(host, command, timeout=None if template is not None else 180))
    port = _tunnel(host, info['port'])
    connection = Connection('127.0.0.1', port, info['token'])
    connection.call('ping')
    return connection


def _wait_stopping(metadata):
    deadline = time.monotonic() + 15
    while metadata.exists():
        try:
            info = json.loads(metadata.read_text())
        except FileNotFoundError:
            return
        if info.get('status') != 'stopping':
            return
        if time.monotonic() >= deadline:
            raise ResourceUnavailable('worker has not completed its acknowledged shutdown')
        time.sleep(.05)


@dataclass
class Endpoint:
    """Worker-local placement for pools; credentials never enter public state."""
    port: int
    token: str

    def connection(self):
        return Connection('127.0.0.1', self.port, self.token)


@dataclass
class Slurm:
    job_id: str
    config: dict
    owned: bool = False
    queue_seconds: float = 0

    @dualclassmethod
    def connect(cls, job_id, **config):
        if not str(job_id).isdigit():
            raise ValueError('Slurm job ID must be numeric')
        return cls(str(job_id), config, owned=False)

    @dualclassmethod
    def acquire(cls, *, gpu=None, cpus=1, memory='4GiB', partition=None, qos=None,
                walltime='1h', account=None, queue_timeout=None, _cancel_event=None, **config):
        positive(cpus, 'cpus', integer=True)
        identifier = 'allocation-' + uuid.uuid4().hex
        directory = home() / 'allocations' / identifier
        directory.mkdir(parents=True, mode=0o700)
        worker_environment = _slurm_environment(config)
        worker_environment.setdefault('PYTHONPATH', str(Path(__file__).resolve().parents[2]))
        command = [config.get('python', sys.executable), '-m', 'sandweave.sandbox.worker',
                   '--metadata', str(directory / 'worker.json')]
        script = directory / 'worker.sh'
        script.write_text('#!/bin/sh\nset -eu\n' +
                          '\n'.join('export ' + shlex.quote(k + '=' + v) for k, v in worker_environment.items()) +
                          '\nexec ' + shlex.join(command) + '\n')
        arguments = ['sbatch', '--parsable', '--no-requeue', '--job-name=sandweave-worker',
                     '--nodes=1', '--ntasks=1', '--cpus-per-task='+str(cpus),
                     '--mem='+str((memory_bytes(memory)+1024**2-1)//1024**2)+'M',
                     '--time='+_walltime(walltime), '--output='+str(directory / 'worker.log')]
        for key, value in (('partition', partition), ('qos', qos), ('account', account)):
            if value is not None:
                arguments.append('--'+key+'='+str(value))
        if gpu:
            arguments.append('--gres=gpu:' + (str(gpu)+':' if gpu is not True else '') + '1')
        result = subprocess.run([*arguments, str(script)], capture_output=True, text=True, check=True)
        job_id = result.stdout.strip().split(';')[0]
        allocation = cls(job_id, {**config, 'metadata': str(directory / 'worker.json'), 'cpus': cpus}, owned=True)
        started = time.monotonic()
        try:
            allocation._wait(queue_timeout, cancel_event=_cancel_event)
        except BaseException:
            allocation.close()
            raise
        allocation.queue_seconds = time.monotonic() - started
        return allocation

    @acquire.async_impl
    async def _acquire_async(cls, **kwargs):
        cancelled = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(cls.acquire, _cancel_event=cancelled, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            try:
                allocation = await asyncio.shield(task)
            except InterruptedError:
                pass
            else:
                await allocation.close.aio()
            raise

    def _job(self):
        result = subprocess.run(['scontrol', 'show', 'job', '-o', self.job_id],
                                capture_output=True, text=True, check=True)
        fields = dict(piece.split('=', 1) for piece in result.stdout.split() if '=' in piece)
        if fields.get('UserId', '').split('(', 1)[-1].rstrip(')') != str(os.getuid()):
            raise ResourceUnavailable('Slurm job is not owned by this user')
        return fields

    def _wait(self, timeout=300, cancel_event=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError('allocation request cancelled')
            job = self._job()
            if job['JobState'] == 'RUNNING':
                return job
            if job['JobState'] not in ('PENDING', 'CONFIGURING'):
                raise ResourceUnavailable('allocation is not runnable: ' + job['JobState'])
            if job.get('Reason') in ('QOSMinGRES', 'BadConstraints', 'InvalidAccount', 'InvalidQOS'):
                raise ResourceUnavailable('scheduler rejected these resource settings: ' + job['Reason'])
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('allocation has not started before its queue deadline')
            time.sleep(.5)

    def connection(self):
        job = self._wait(self.config.get('queue_timeout', 300))
        if job.get('NumNodes') != '1':
            raise ResourceUnavailable('select one worker per single-node Slurm allocation')
        hostname = subprocess.check_output(['scontrol', 'show', 'hostnames', job['NodeList']], text=True).strip()
        directory = home() / 'allocations' / ('job-' + self.job_id)
        if 'metadata' not in self.config:
            source = assets(directory=self.config.get('home'), selected=self.config.get('assets'))
            directory = directory / (asset_identity(source) + '-' + software_identity()[:16])
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = Path(self.config.get('metadata', directory / 'worker.json'))
        # Existing-job placement starts one persistent step in that allocation.
        # The worker never inherits CPUs/GPUs from the SSH daemon's environment.
        if 'metadata' not in self.config:
            with locked(directory / 'startup.lock'):
                process = None
                _wait_stopping(metadata)
                info = json.loads(metadata.read_text()) if metadata.exists() else None
                if info is not None and info.get('hostname') == hostname:
                    # Health failure alone does not permit replacing a live
                    # worker. Check its exact process ownership before restart.
                    port = info['port'] if hostname == socket.gethostname() else _tunnel(self.config.get('host', hostname), info['port'])
                    probe = Connection('127.0.0.1', port, info['token'], timeout=2)
                    try:
                        probe.call('ping')
                    except Exception:
                        script = ('from pathlib import Path; p=Path(' + repr('/proc/' + str(info['pid']) + '/cmdline') +
                                  '); a=p.read_bytes().split(bytes([0])) if p.exists() else []; print(int(' +
                                  repr(b'sandweave.sandbox.worker') + ' in a and ' + repr(str(metadata).encode()) + ' in a))')
                        command = [self.config.get('python', sys.executable), '-c', script]
                        live = (subprocess.check_output(command, text=True) if hostname == socket.gethostname()
                                else _ssh(self.config.get('host', hostname), command)).strip() == '1'
                        if live:
                            raise ResourceUnavailable('Slurm worker is alive but unreachable; inspect its private worker log')
                        info = None
                    finally:
                        probe.close()
                if info is None or info.get('hostname') != hostname:
                    metadata.unlink(missing_ok=True)
                    environment = {**os.environ, **_slurm_environment(self.config)}
                    environment.setdefault('PYTHONPATH', str(Path(__file__).resolve().parents[2]))
                    cpus = self.config.get('cpus', int(job['NumCPUs']))
                    command = ['srun', '--jobid='+self.job_id, '--overlap', '--ntasks=1', '--cpus-per-task='+str(cpus),
                               self.config.get('python', sys.executable), '-m', 'sandweave.sandbox.worker',
                               '--metadata', str(metadata)]
                    with (directory / 'worker.log').open('ab') as log:
                        process = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL,
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                    atomic_json(directory / 'launcher.json', {'pid': process.pid, 'job_id': self.job_id})
                deadline = time.monotonic() + 180
                while not metadata.exists():
                    if process is not None and process.poll() is not None:
                        raise ResourceUnavailable('Slurm worker exited during startup; inspect ' + str(directory / 'worker.log'))
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Slurm worker is still preparing; inspect ' + str(directory / 'worker.log'))
                    time.sleep(.1)
        else:
            deadline = time.monotonic() + 180
            while not metadata.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError('allocated worker did not publish readiness metadata')
                time.sleep(.1)
        info = json.loads(metadata.read_text())
        if hostname == socket.gethostname():
            port = info['port']
        else:
            port = _tunnel(self.config.get('host', hostname), info['port'])
        connection = Connection('127.0.0.1', port, info['token'])
        connection.call('ping')
        return connection

    @dualmethod
    def close(self):
        if self.owned:
            # The explicit allocation scope owns its job; borrowed targets do not.
            subprocess.run(['scancel', self.job_id], check=True)
            self.owned = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close.aio()


def _walltime(value):
    import re
    if re.fullmatch(r'\d+[hm]', str(value)):
        return str(int(value[:-1]) * (60 if value[-1] == 'h' else 1))
    if re.fullmatch(r'(?:\d+-)?\d+:\d{2}(?::\d{2})?', str(value)):
        return str(value)
    raise ValueError('walltime must be a duration such as 3h, 30m, or HH:MM:SS')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--ensure', action='store_true', required=True)
    parser.add_argument('--template')
    args = parser.parse_args()
    from ..templates.resolve import Template
    connection = local_connection(template=Template(args.template).resolve() if args.template else None)
    information = connection.call('ping')
    print(json.dumps({**information, 'port': connection.port, 'token': connection.token}))
    connection.close()

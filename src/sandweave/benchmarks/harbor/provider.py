"""Harbor's environment protocol backed by direct Sandweave sandboxes."""
import asyncio
import copy
from contextvars import ContextVar
from dataclasses import asdict
import hashlib
from functools import partial
import json
from pathlib import Path
import shlex
import tempfile
import tarfile
import uuid

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import EnvironmentCapabilities, EnvironmentResourceCapabilities
from harbor.environments.tar_transfer import extract_dir_from_file
from harbor.environments.definition import require_agent_environment_definition, should_use_prebuilt_docker_image
from harbor.models.trial.config import ResourceMode

from ...sandbox.asyncio import dualmethod
from ...sandbox.files import Files
from ...sandbox.resources import CPU, GPU, Memory, Network
from ...sandbox.sandbox import Sandbox
from ..benchmark import drained, settled
from .runner import current_session


def pack_inputs(source, archive):
    """Match docker cp's contents transfer without importing host user IDs."""
    source = Path(source)
    if not source.is_dir():
        raise FileNotFoundError(source)
    def ownership(member):
        member.uid = member.gid = 0
        member.uname = member.gname = ''
        return member
    with tarfile.open(archive, 'w:gz') as output:
        for path in sorted(source.iterdir()):
            output.add(path, arcname=path.name, filter=ownership)


class AgentSandbox(Sandbox):
    """An independent client handle carrying the current Harbor agent defaults."""
    defaults = None
    scoped_env = None

    def _defaults(self, kwargs):
        merged = {**(self.defaults or {}), **{key: value for key, value in kwargs.items()
                  if value is not None or key not in ('user', 'cwd')}}
        merged['env'] = {**((self.defaults or {}).get('env') or {}), **(kwargs.get('env') or {}),
                         **(self.scoped_env or {})}
        return merged

    @dualmethod
    def exec(self, command=None, **kwargs):
        return Sandbox.exec(self, command, **self._defaults(kwargs))

    @exec.async_impl
    async def _exec_async(self, command=None, **kwargs):
        return await Sandbox.__dict__['exec'].__get__(self, Sandbox).aio(command, **self._defaults(kwargs))


class SandweaveEnvironment(BaseEnvironment):
    _exec_shell = '/bin/bash'

    def __init__(self, *args, target=None, **kwargs):
        self.sandbox = None
        self.lease = None
        self.built_image = None
        self.project = None
        self._stopping = None
        self.target = target
        self.session = current_session.get()
        super().__init__(*args, **kwargs)
        if self.session is not None:
            self.session.environments.add(self)

    @staticmethod
    def type():
        return 'sandweave'

    @property
    def _uses_compose(self):
        return (self.environment_dir / 'docker-compose.yaml').exists() or bool(self.extra_docker_compose_paths)

    @property
    def capabilities(self):
        return EnvironmentCapabilities(gpus=True, disable_internet=True, dynamic_network_policy=True,
            docker_compose=True, network_allowlist=True, network_allowlist_hostnames=True,
            network_allowlist_wildcard_hostnames=True, network_allowlist_ipv4_addresses=True,
            network_allowlist_ipv4_cidrs=True)

    @classmethod
    def resource_capabilities(cls):
        return EnvironmentResourceCapabilities(cpu_limit=True, memory_limit=True)

    def _validate_definition(self):
        require_agent_environment_definition(self.environment_dir,
            docker_image=self.task_env_config.docker_image,
            extra_docker_compose_paths=self.extra_docker_compose_paths)
        if self._effective_gpus > 1:
            raise ValueError('Sandweave currently supports one GPU per sandbox')

    async def _apply_network_policy(self, network_policy):
        if self.sandbox is not None:
            network = self.network(network_policy)
            sandboxes = self.project.views.values() if self.project else [self.sandbox]
            await asyncio.gather(*(drained(partial(sandbox._call, 'network_policy', network=asdict(network)))
                                   for sandbox in sandboxes))

    @staticmethod
    def network(policy):
        modes = {'no-network': 'offline', 'public': 'internet', 'allowlist': 'allowlist'}
        return Network(modes[policy.network_mode], allowed_hosts=tuple(policy.allowed_hosts))

    def options(self):
        config = self.task_env_config
        image = config.docker_image
        image = (image if image.startswith('docker://') else 'docker://' + image) if image else None
        template = {'name': 'harbor', 'command_shell': '/bin/bash'}
        if config.workdir:
            template['workdir'] = config.workdir
        network = self.network(self._network_policy)
        quota = self._resource_limit_value('cpu', auto_mode=ResourceMode.LIMIT)
        return dict(image=image, template=template, cpu=CPU(self._effective_cpus or 1, quota=quota),
                    memory=Memory(f'{self._effective_memory_mb or 1024}MiB'),
                    gpu=GPU(model=tuple(config.gpu_types) or None) if self._effective_gpus and config.gpu_types
                        else bool(self._effective_gpus), network=network,
                    env=self._startup_env(), startup_timeout=config.build_timeout_sec)

    async def start(self, force_build=False):
        if self.sandbox is not None:
            return
        if self._uses_compose:
            from .compose import Project, render
            try:
                self.project = Project(self, await drained(partial(render, self, force_build)))
                self.sandbox = await self.project.start(force_build)
                await self._prepare_paths()
                await self._upload_environment_dir_after_start()
                return
            except BaseException:
                await self.stop(delete=True)
                raise
        options = self.options()
        try:
            if not should_use_prebuilt_docker_image(self.environment_dir,
                    docker_image=self.task_env_config.docker_image, force_build=force_build):
                from ...templates.build import build
                settings = dict(template=options['template'], timeout=self.task_env_config.build_timeout_sec,
                                force=force_build)
                if self.session:
                    snapshot = await self.session.pool.build(self.environment_dir, settings)
                else:
                    snapshot = self.built_image = await drained(partial(build,
                        self.environment_dir, target=self.target, **settings))
                options.pop('image')
                options.pop('template')
                options['cache'] = snapshot.id
            if self.session:
                if options.get('image'):
                    options['image'] = await self.session.pool.pin(options['image'])
                # Settings participate even when two task directories have the
                # same content hash. Environment input files are copied later,
                # into each pristine lease, never into a shared task baseline.
                identity = json.dumps(options, sort_keys=True, default=asdict)
                key = hashlib.sha256(identity.encode()).hexdigest()
                # An intermediate separate verifier runs while the agent keeps
                # its lease. Its capacity cannot depend on the agent releasing
                # that same image pool. Images still share the build/pin cache.
                role = 'agent' if self is self.session.trial.agent_environment else 'verifier'
                pool = await self.session.pool.environment_pool((role, key), options)
                lease = pool.acquire()
                pending = asyncio.create_task(self.session.pool.blocking(lease.__enter__))
                try:
                    self.sandbox = await asyncio.shield(pending)
                    self.lease = lease
                except asyncio.CancelledError:
                    lease.cancelled.set()
                    try:
                        self.sandbox = await settled(pending)
                        self.lease = lease
                    except Exception:
                        pass
                    raise
            else:
                self.sandbox = await Sandbox.create.aio(target=self.target, **options)
            # Harbor owns these paths and collects their contents before stop.
            # They are not host binds when the provider advertises mounted=False.
            await self._prepare_paths()
            settings = self.sandbox.spec.get('image', {}).get('settings', {})
            entrypoint = settings.get('entrypoint') or []
            process = await self.sandbox.exec.aio(argv=[*entrypoint, 'sh', '-c', 'sleep infinity'])
            await drained(partial(self.sandbox._call, 'service_setup', settings={'watch_process': process.id}))
            await self._upload_environment_dir_after_start()
        except BaseException:
            await self.stop(delete=True)
            raise

    async def stop(self, delete=True):
        # Harbor shields cleanup on cancellation. Keep its task reachable so
        # the pull lease can drain it before returning capacity to the pool.
        if self._stopping is None:
            self._stopping = asyncio.create_task(self._stop(delete))
        await settled(self._stopping)

    async def _stop(self, delete):
        sandbox, self.sandbox = self.sandbox, None
        lease, self.lease = self.lease, None
        try:
            if self.project is not None:
                await self.project.close()
                self.project = None
            elif lease is not None:
                await lease.__aexit__(None, None, None)
            elif sandbox is not None:
                try:
                    await sandbox.terminate.aio()
                finally:
                    sandbox._close_connection()
        finally:
            if self.built_image is not None:
                self.built_image._connection.close()
                self.built_image = None

    async def _prepare_paths(self):
        # A declared workdir may not exist yet. Bootstrap from / before any
        # helper inherits that workdir, including a separate verifier's /tests.
        if self.task_env_config.workdir:
            await self._checked('mkdir -p -- ' + shlex.quote(self.task_env_config.workdir))
        paths = self._mount_targets(writable_only=True)
        if paths:
            await self._checked(self._ensure_dirs_command(paths))

    def agent_view(self):
        view = AgentSandbox.__new__(AgentSandbox)
        view.__dict__ = {**self.sandbox.__dict__, '_owned': False, '_controls': {},
                         '_connection': self.sandbox._connection.clone()}
        view.files = Files(view)
        return view

    def _service(self, service):
        if self.project is None or service not in self.project.views:
            raise ValueError('unknown Compose service: ' + str(service))
        provider = copy.copy(self)
        provider.sandbox = self.project.views[service]
        provider.default_user = None
        provider._persistent_env = {}
        provider._exec_env_overlays = ContextVar('harbor_service_env', default=())
        provider._exec_shell = '/bin/sh'
        provider.task_env_config = self.task_env_config.model_copy(update={'env': {}, 'workdir': None})
        return provider

    async def service_exec(self, command, *, service=None, cwd=None, env=None, timeout_sec=None, user=None):
        provider = self if service is None or self.is_main_service(service) else self._service(service)
        return await provider.exec(command, cwd=cwd, env=env, timeout_sec=timeout_sec, user=user)

    async def service_download_file(self, source_path, target_path, *, service=None):
        provider = self if service is None or self.is_main_service(service) else self._service(service)
        return await provider.download_file(source_path, target_path)

    async def service_download_dir(self, source_dir, target_dir, *, service=None):
        provider = self if service is None or self.is_main_service(service) else self._service(service)
        return await provider.download_dir(source_dir, target_dir)

    async def stop_service(self, service):
        if self.project is None or service not in self.project.processes:
            raise ValueError('unknown Compose service: ' + service)
        import signal
        from .compose import duration
        process = self.project.processes[service]
        settings = self.project.config['services'][service]
        selected = str(settings.get('stop_signal', 'SIGTERM'))
        number = int(selected) if selected.isdigit() else getattr(signal,
            selected if selected.startswith('SIG') else 'SIG' + selected)
        await self.project.views[service]._acall('process_terminate', process_id=process.id, signal_number=number)
        try:
            await process.wait.aio(timeout=duration(settings.get('stop_grace_period'), 10))
        except TimeoutError:
            await process.terminate.aio()
            await process.wait.aio()

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None, _maintenance=False):
        if self.sandbox is None:
            raise RuntimeError('Harbor environment has not started')
        process = await self.sandbox.exec.aio(command, cwd=cwd or self.task_env_config.workdir,
            env=self._merge_env(env), user=self._resolve_user(user), timeout=timeout_sec, shell=self._exec_shell,
            _maintenance=_maintenance or getattr(self, '_file_transfer', False))
        try:
            await process.stdin.close.aio()
            callback = self._output_callback()
            if callback is not None:
                async def consume(stream, name):
                    chunks = []
                    async for text in stream:
                        chunks.append(text)
                        await callback(text, name)
                    return ''.join(chunks)
                pending = [asyncio.create_task(consume(process.stdout, 'stdout')),
                           asyncio.create_task(consume(process.stderr, 'stderr')),
                           asyncio.create_task(process.wait.aio())]
                try:
                    stdout, stderr, _ = await asyncio.gather(*pending)
                finally:
                    for operation in pending:
                        operation.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                return ExecResult(stdout=stdout, stderr=stderr, return_code=await process.poll.aio())
            await process.wait.aio()
            stdout, stderr = await asyncio.gather(process.stdout.read.aio(), process.stderr.read.aio())
            return ExecResult(stdout=stdout, stderr=stderr, return_code=await process.poll.aio())
        except BaseException:
            try:
                await process.terminate.aio()
            except Exception:
                pass  # Preserve timeout/callback errors after stopping the command.
            raise

    async def _checked(self, command):
        result = await self.exec(command, cwd='/', user='root', timeout_sec=600, _maintenance=True)
        if result.return_code:
            raise RuntimeError(f'Harbor file transfer failed ({result.return_code}): {result.stderr}')

    def _transfer_view(self):
        provider = copy.copy(self)
        provider._file_transfer = True
        return provider

    async def is_dir(self, path, user=None):
        return await BaseEnvironment.is_dir(self._transfer_view(), path, user=user)

    async def is_file(self, path, user=None):
        return await BaseEnvironment.is_file(self._transfer_view(), path, user=user)

    async def service_is_dir(self, path, *, service=None, user=None):
        return await BaseEnvironment.service_is_dir(self._transfer_view(), path, service=service, user=user)

    async def _download_dir_with_exclusions_impl(self, **kwargs):
        return await BaseEnvironment._download_dir_with_exclusions_impl(self._transfer_view(), **kwargs)

    async def download_dir_filtered(self, **kwargs):
        return await BaseEnvironment.download_dir_filtered(self._transfer_view(), **kwargs)

    async def upload_file(self, source_path, target_path):
        source = Path(source_path)
        await drained(self.sandbox.files.upload, source, target_path)
        await self._checked(f'chmod {source.stat().st_mode & 0o777:o} -- {shlex.quote(str(target_path))}')

    async def download_file(self, source_path, target_path):
        await drained(self.sandbox.files.download, str(source_path), Path(target_path))

    async def upload_dir(self, source_dir, target_dir):
        remote = '/.sandweave-runtime/harbor-' + uuid.uuid4().hex + '.tar.gz'
        with tempfile.TemporaryDirectory(prefix='sandweave-harbor-') as directory:
            archive = Path(directory) / 'transfer.tar.gz'
            await drained(pack_inputs, source_dir, archive)
            try:
                await drained(self.sandbox.files.upload, archive, remote)
                await self._checked(f'mkdir -p -- {shlex.quote(str(target_dir))} && '
                                    f'tar -xzf {shlex.quote(remote)} -C {shlex.quote(str(target_dir))}')
            finally:
                await self._checked('rm -f -- ' + shlex.quote(remote))

    async def download_dir(self, source_dir, target_dir):
        remote = '/.sandweave-runtime/harbor-' + uuid.uuid4().hex + '.tar.gz'
        with tempfile.TemporaryDirectory(prefix='sandweave-harbor-') as directory:
            archive = Path(directory) / 'transfer.tar.gz'
            try:
                await self._checked(f'tar -czf {shlex.quote(remote)} -C {shlex.quote(str(source_dir))} .')
                await drained(self.sandbox.files.download, remote, archive)
                await drained(extract_dir_from_file, archive, target_dir)
            finally:
                await self._checked('rm -f -- ' + shlex.quote(remote))

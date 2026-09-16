"""Harbor's environment protocol backed by direct Sandweave sandboxes."""
import asyncio
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import shlex
import tempfile
import uuid

from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.environments.capabilities import EnvironmentCapabilities, EnvironmentResourceCapabilities
from harbor.environments.tar_transfer import pack_dir_to_file, extract_dir_from_file

from ...sandbox.asyncio import dualmethod
from ...sandbox.files import Files
from ...sandbox.resources import CPU, Memory, Network
from ...sandbox.sandbox import Sandbox
from ..benchmark import drained, settled
from .runner import current_session


class AgentSandbox(Sandbox):
    """An independent client handle carrying the current Harbor agent defaults."""
    defaults = None

    def _defaults(self, kwargs):
        merged = {**(self.defaults or {}), **{key: value for key, value in kwargs.items()
                  if value is not None or key not in ('user', 'cwd')}}
        merged['env'] = {**((self.defaults or {}).get('env') or {}), **(kwargs.get('env') or {})}
        return merged

    @dualmethod
    def exec(self, command=None, **kwargs):
        return Sandbox.exec(self, command, **self._defaults(kwargs))

    @exec.async_impl
    async def _exec_async(self, command=None, **kwargs):
        return await Sandbox.__dict__['exec'].__get__(self, Sandbox).aio(command, **self._defaults(kwargs))


class SandweaveEnvironment(BaseEnvironment):
    def __init__(self, *args, target=None, **kwargs):
        self.sandbox = None
        self.lease = None
        self.target = target
        self.session = current_session.get()
        super().__init__(*args, **kwargs)

    @staticmethod
    def type():
        return 'sandweave'

    @property
    def capabilities(self):
        return EnvironmentCapabilities(gpus=True, disable_internet=True)

    @classmethod
    def resource_capabilities(cls):
        return EnvironmentResourceCapabilities(cpu_request=True, memory_request=True, memory_limit=True)

    def _validate_definition(self):
        if (self.environment_dir / 'docker-compose.yaml').exists():
            raise ValueError('Sandweave does not yet implement Harbor Compose service groups')
        if not self.task_env_config.docker_image:
            raise ValueError('Harbor task needs a prebuilt docker_image; Dockerfile builds are not yet available')
        if self._effective_gpus > 1:
            raise ValueError('Sandweave currently supports one GPU per sandbox')
        if any(policy != self.network_policy for policy in self._phase_network_policies):
            raise ValueError('Sandweave does not yet support changing network policy between Harbor phases')

    def options(self):
        config = self.task_env_config
        image = config.docker_image
        image = image if image.startswith('docker://') else 'docker://' + image
        template = {'name': 'harbor', 'command_shell': '/bin/bash'}
        if config.workdir:
            template['workdir'] = config.workdir
        network = Network('offline' if self._network_disabled else 'internet')
        return dict(image=image, template=template, cpu=CPU(self._effective_cpus or 1),
                    memory=Memory(f'{self._effective_memory_mb or 1024}MiB'),
                    gpu=bool(self._effective_gpus), network=network,
                    env=self._startup_env(), startup_timeout=config.build_timeout_sec)

    async def start(self, force_build=False):
        if self.sandbox is not None:
            return
        if force_build and (self.environment_dir / 'Dockerfile').exists():
            raise ValueError('force_build requires a Dockerfile builder; Sandweave cannot silently reuse the image')
        options = self.options()
        try:
            if self.session:
                options['image'] = await self.session.pool.pin(options['image'])
                # Settings participate even when two task directories have the
                # same content hash. Environment input files are copied later,
                # into each pristine lease, never into a shared task baseline.
                identity = json.dumps(options, sort_keys=True, default=asdict)
                key = hashlib.sha256(identity.encode()).hexdigest()
                pool = await self.session.pool.environment_pool(key, options)
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
            paths = [mount['target'] for mount in self._mounts]
            if self.task_env_config.workdir:
                paths.append(self.task_env_config.workdir)
            if paths:
                await self._checked('mkdir -p -- ' + ' '.join(map(shlex.quote, paths)))
            settings = self.sandbox.spec.get('image', {}).get('settings', {})
            entrypoint = settings.get('entrypoint') or []
            if entrypoint:
                await self.sandbox.exec.aio(argv=[*entrypoint, 'sh', '-c', 'sleep infinity'])
            await self._upload_environment_dir_after_start()
        except BaseException:
            await self.stop(delete=True)
            raise

    async def stop(self, delete=True):
        sandbox, self.sandbox = self.sandbox, None
        lease, self.lease = self.lease, None
        if lease is not None:
            await lease.__aexit__(None, None, None)
        elif sandbox is not None:
            try:
                await sandbox.terminate.aio()
            finally:
                sandbox._close_connection()

    def agent_view(self):
        view = AgentSandbox.__new__(AgentSandbox)
        view.__dict__ = {**self.sandbox.__dict__, '_owned': False, '_controls': {},
                         '_connection': self.sandbox._connection.clone()}
        view.files = Files(view)
        return view

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        if self.sandbox is None:
            raise RuntimeError('Harbor environment has not started')
        process = await self.sandbox.exec.aio(command, cwd=cwd or self.task_env_config.workdir,
            env=self._merge_env(env), user=self._resolve_user(user), timeout=timeout_sec, shell='/bin/bash')
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
            await process.terminate.aio()
            raise

    async def _checked(self, command):
        result = await self.exec(command, cwd='/', user='root', timeout_sec=600)
        if result.return_code:
            raise RuntimeError(f'Harbor file transfer failed ({result.return_code}): {result.stderr}')

    async def upload_file(self, source_path, target_path):
        source = Path(source_path)
        await drained(self.sandbox.files.upload, source, target_path)
        await self._checked(f'chmod {source.stat().st_mode & 0o777:o} -- {shlex.quote(str(target_path))}')

    async def download_file(self, source_path, target_path):
        await drained(self.sandbox.files.download, str(source_path), Path(target_path))

    async def upload_dir(self, source_dir, target_dir):
        remote = '/tmp/.sandweave-harbor-' + uuid.uuid4().hex + '.tar.gz'
        with tempfile.TemporaryDirectory(prefix='sandweave-harbor-') as directory:
            archive = Path(directory) / 'transfer.tar.gz'
            await drained(pack_dir_to_file, source_dir, archive)
            try:
                await drained(self.sandbox.files.upload, archive, remote)
                await self._checked(f'mkdir -p -- {shlex.quote(str(target_dir))} && '
                                    f'tar -xzf {shlex.quote(remote)} -C {shlex.quote(str(target_dir))}')
            finally:
                await self._checked('rm -f -- ' + shlex.quote(remote))

    async def download_dir(self, source_dir, target_dir):
        remote = '/tmp/.sandweave-harbor-' + uuid.uuid4().hex + '.tar.gz'
        with tempfile.TemporaryDirectory(prefix='sandweave-harbor-') as directory:
            archive = Path(directory) / 'transfer.tar.gz'
            try:
                await self._checked(f'tar -czf {shlex.quote(remote)} -C {shlex.quote(str(source_dir))} .')
                await drained(self.sandbox.files.download, remote, archive)
                await drained(extract_dir_from_file, archive, target_dir)
            finally:
                await self._checked('rm -f -- ' + shlex.quote(remote))

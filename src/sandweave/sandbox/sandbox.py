"""A Sandbox is a running template, with one public lifecycle and command API."""
import copy
import asyncio
from dataclasses import asdict
import time
import uuid

from .asyncio import dualmethod, dualclassmethod
from .errors import CommandError, UnsupportedFeature
from .files import Files
from .mounts import serialize as mount_spec
from .process import Process
from .resources import normalize, positive, CPU, GPU, Memory, Network
from .snapshots import SnapshotRef
from .targets import connect
from ..templates.resolve import Template, setup_step


def definition(*, template=None, image=None, setup=None, cache=None, snapshot=None, cache_key=None,
               cpu=None, memory=None, gpu=None, network=None, target=None, runtime='gvisor',
               env=None, mounts=None, name=None, ttl=None, detached=False, startup_timeout=300,
               keep_on_error=False, refresh=False, experimental_gpu_live=False):
    """Resolve a complete portable definition without starting an environment."""
    if sum(x is not None for x in (cache, snapshot)) > 1:
        raise ValueError('cache and snapshot are alternative sources')
    reference = cache if cache is not None else snapshot
    if reference is not None and any(x is not None for x in (template, image, setup, cache_key)):
        raise ValueError('a saved source cannot be combined with a new recipe')
    if type(detached) is not bool:
        raise ValueError('detached must be a bool')
    if ttl is not None:
        positive(ttl, 'ttl')
    positive(startup_timeout, 'startup_timeout')
    saved = None
    if reference is not None:
        connection = connect(target)
        try:
            saved = connection.call('snapshot_spec', reference=str(reference))
        finally:
            connection.close()
    if saved:
        reference = saved['reference']
        recipe = copy.deepcopy(saved['spec']['template'])
        defaults = saved['spec']['resources']
        defaults = {'cpu': CPU(**defaults['cpu']), 'memory': Memory(**defaults['memory']),
                    'gpu': GPU(**defaults['gpu']) if defaults['gpu'] else False,
                    'network': Network(**defaults['network'])}
        if env is None:
            env = saved['spec']['env']
        if mounts is None:
            mounts = saved['spec']['mounts']
        runtime = saved['spec']['runtime']
    else:
        recipe = Template(template if template is not None else ({} if image else 'coding')).resolve()
        defaults = recipe['resources']
    selected_image = image if image is not None else recipe.get('image')
    if refresh and cache_key is None and selected_image is None:
        raise ValueError('refresh requires cache_key or image')
    if selected_image is not None:
        from ..templates.images import validate
        validate(selected_image)
        if runtime != 'gvisor':
            raise UnsupportedFeature('Docker images currently require runtime="gvisor"')
        recipe['image'] = selected_image
    if setup:
        recipe['setup_steps'].append(setup_step(setup))
    selected_memory = memory if memory is not None else defaults.get('memory', '1GiB')
    if not isinstance(selected_memory, Memory):
        selected_memory = Memory(selected_memory, defaults.get('runtime_memory', '512MiB'))
    resources = normalize(cpu=cpu if cpu is not None else defaults.get('cpu', 1), memory=selected_memory,
                          gpu=gpu if gpu is not None else defaults.get('gpu', False),
                          network=network if network is not None else defaults.get('network', 'internet'))
    spec = {'template': recipe, 'resources': resources, 'runtime': runtime,
            'env': {**recipe.get('env', {}), **(env or {})}, 'mounts': mount_spec(mounts), 'name': name,
            'ttl': ttl, 'detached': detached, 'startup_timeout': startup_timeout, 'keep_on_error': keep_on_error,
            'experimental_gpu_live': experimental_gpu_live}
    if saved and saved['spec'].get('image'):
        spec['image'] = copy.deepcopy(saved['spec']['image'])
    elif selected_image is not None:
        spec['image'] = {'reference': selected_image}
    return dict(spec=spec, reference=reference, cache_key=cache_key, refresh=refresh)


class Sandbox:
    def __init__(self, *, template=None, image=None, setup=None, cache=None, snapshot=None, cache_key=None,
                 cpu=None, memory=None, gpu=None, network=None, target=None, runtime='gvisor',
                 env=None, mounts=None, name=None, ttl=None, detached=False, startup_timeout=300, keep_on_error=False,
                 refresh=False, experimental_gpu_live=False):
        options = dict(locals()); options.pop('self')
        self._connection = None
        try:
            self._initialize(**options)
        except BaseException:
            if self._connection is not None:
                self._connection.close()
            raise

    def _initialize(self, *, template=None, image=None, setup=None, cache=None, snapshot=None, cache_key=None,
                    cpu=None, memory=None, gpu=None, network=None, target=None, runtime='gvisor',
                    env=None, mounts=None, name=None, ttl=None, detached=False, startup_timeout=300, keep_on_error=False,
                    refresh=False, experimental_gpu_live=False):
        options = dict(locals()); options.pop('self')
        request = definition(**options)
        spec, recipe, reference = request['spec'], request['spec']['template'], request['reference']
        self.id = ('vr-sw-' if 'vr' in recipe['capabilities'] else 'sw-') + uuid.uuid4().hex
        self._owned, self._closed, self._terminated = True, False, False
        self._target = target
        operation_id = uuid.uuid4().hex
        if self._connection is not None:
            self._connection.close()
        # Select the worker after installing the recipe's dependencies. A
        # saved recipe needs the same check when restored on another worker.
        self._connection = connect(target, template=recipe)
        initial = self._connection
        self._connection = initial.clone(timeout=startup_timeout + 60)
        initial.close()
        from .ownership import client_owner
        owner = None if detached else client_owner(self._connection)
        self._info = self._connection.call('create', identity=self.id, spec=spec, operation_id=operation_id,
                                          reference=reference, cache_key=cache_key, refresh=refresh, owner=owner)
        self.files = Files(self)
        self._controls = {}

    @dualclassmethod
    def create(cls, **kwargs):
        return cls(**kwargs)

    @create.async_impl
    async def _create_async(cls, **kwargs):
        operation = asyncio.create_task(asyncio.to_thread(cls, **kwargs))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            # Reconcile the in-flight create before returning cancellation, so
            # an acknowledged success cannot leave an orphaned owned sandbox.
            instance = await asyncio.shield(operation)
            try:
                await instance.terminate.aio()
            finally:
                instance.close()
            raise

    @dualclassmethod
    def connect(cls, identity, *, target=None):
        self = cls.__new__(cls)
        self._connection = connect(target)
        self.id = str(identity)
        self._owned, self._closed, self._terminated = False, False, False
        self._target = target
        try:
            self._info = self._connection.call('describe', identity=self.id)
        except BaseException:
            self._connection.close()
            raise
        self.id = self._info['id']
        self.files = Files(self)
        self._controls = {}
        return self

    def _call(self, operation, **kwargs):
        if self._closed:
            raise RuntimeError('sandbox client is closed; connect with its ID to attach again')
        return self._connection.call(operation, identity=self.id, **kwargs)

    @property
    def spec(self):
        return copy.deepcopy(self._info['spec'])

    @property
    def info(self):
        """Fetch state, configured resources, selected GPUs and worker-local VNC.

        CPU and memory describe settings, not live utilization. VNC URLs use
        the worker's loopback address.
        """
        from .information import summarize
        value = summarize(self.status())
        if value['vnc'] is not None:
            from ..templates.gnome.controls import vnc_password
            value['vnc']['password'] = vnc_password(self)
        return value

    @property
    def timings(self):
        return dict(self._info['timings'])

    @property
    def capabilities(self):
        return copy.deepcopy(self._info.get('capabilities', {}))

    def capability(self, name):
        from ..templates.controls import provider, implementation
        config = self._info['spec']['template']['capabilities'].get(name)
        if config is None:
            raise UnsupportedFeature('this template does not provide ' + name + ' controls')
        if name not in self._controls:
            expected = self._info.get('capabilities', {}).get(name, {}).get('implementation')
            if expected is not None and implementation(config.get('provider', name)) != expected:
                raise UnsupportedFeature('client and worker control provider versions differ: ' + name)
            self._controls[name] = provider(config.get('provider', name)).bind(self, config)
        return self._controls[name]

    @property
    def desktop(self):
        return self.capability('desktop')

    @property
    def vr(self):
        return self.capability('vr')

    def __getattr__(self, name):
        # Installed template controls get the same convenient attribute access.
        information = self.__dict__.get('_info', {})
        if name in information.get('capabilities', {}):
            return self.capability(name)
        raise AttributeError(name)

    @dualmethod
    def status(self):
        self._info = self._call('describe')
        return self._info

    @dualmethod
    def exec(self, command=None, *, argv=None, cwd=None, env=None, user=None,
             timeout=None, shell=None, binary=False, max_output_bytes=None, pty=False):
        identity = uuid.uuid4().hex
        self._call('command_start', process_id=identity, command=command, argv=argv,
                    cwd=cwd, env=env, user=user, timeout=timeout, shell=shell, max_output_bytes=max_output_bytes,
                    **({'pty': pty} if pty else {}))
        return Process(self, identity, binary=binary)

    @exec.async_impl
    async def _exec_async(self, *args, **kwargs):
        operation = asyncio.create_task(asyncio.to_thread(self.exec, *args, **kwargs))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            process = await asyncio.shield(operation)
            await process.terminate.aio()
            raise

    @dualmethod
    def run(self, command=None, *, argv=None, cwd=None, env=None, user=None,
            timeout=None, shell=None, check=False, binary=False, max_output_bytes=None, pty=False):
        """Return output and exit status; check=True raises on a nonzero exit."""
        process = self.exec(command, argv=argv, cwd=cwd, env=env, user=user,
                            timeout=timeout, shell=shell, binary=binary, max_output_bytes=max_output_bytes, pty=pty)
        process.stdin.close()
        process.wait()
        result = process.result()
        if check and result.returncode:
            raise CommandError(f'command exited with {result.returncode}', result=result, operation_id=process.id)
        return result

    @run.async_impl
    async def _run_async(self, command=None, *, check=False, **kwargs):
        process = await self.exec.aio(command, **kwargs)
        try:
            await process.stdin.close.aio()
            await process.wait.aio()
            result = await asyncio.to_thread(process.result)
            if check and result.returncode:
                raise CommandError(f'command exited with {result.returncode}', result=result, operation_id=process.id)
            return result
        except asyncio.CancelledError:
            await process.terminate.aio()
            raise

    @dualmethod
    def setup(self, path, *, inputs=(), user='root'):
        return self._call('setup', step=setup_step(path, inputs=inputs, user=user))

    @dualmethod
    def cache(self, key, *, state='filesystem', experimental_gpu_live=False):
        return SnapshotRef.from_record(self._call('capture', state=state, key=key,
            experimental_gpu_live=experimental_gpu_live), self._connection)

    @dualmethod
    def snapshot(self, *, state='memory', experimental_gpu_live=False):
        return SnapshotRef.from_record(self._call('capture', state=state,
            experimental_gpu_live=experimental_gpu_live), self._connection)

    @dualmethod
    def stop(self, *, state='auto', experimental_gpu_live=False):
        saved = self._call('stop', state=state, experimental_gpu_live=experimental_gpu_live)
        self._terminated = True
        return SnapshotRef.from_record(saved, self._connection)

    @dualmethod
    def pause(self):
        self._info = self._call('pause')
        return self._info

    @dualmethod
    def resume(self):
        self._info = self._call('resume')
        return self._info

    @dualmethod
    def terminate(self):
        if not self._terminated:
            # Explicit cleanup remains possible after close(), including an
            # owned context whose client was disconnected inside its body.
            self._info = self._connection.call('terminate', identity=self.id)
            self._terminated = True

    @dualmethod
    def close(self):
        if not self._closed:
            self._connection.close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        try:
            if self._owned and not self._terminated:
                self.terminate()
        finally:
            self.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        try:
            if self._owned and not self._terminated:
                await self.terminate.aio()
        finally:
            await self.close.aio()

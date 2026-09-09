"""A Sandbox is a running template, with one public lifecycle and command API."""
import copy
from dataclasses import asdict
import time
import uuid

from .asyncio import dualmethod, dualclassmethod
from .errors import CommandError, UnsupportedFeature
from .files import Files
from .process import Process
from .resources import normalize
from .targets import connect
from ..templates.resolve import Template, setup_step


class Sandbox:
    def __init__(self, *, template=None, setup=None, cache=None, snapshot=None, cache_key=None,
                 cpu=None, memory=None, gpu=None, network=None, target=None, runtime='gvisor',
                 env=None, mounts=None, name=None, ttl=None, startup_timeout=300, keep_on_error=False,
                 refresh=False):
        if cache is not None or snapshot is not None or cache_key is not None:
            raise UnsupportedFeature('cache/restore is being implemented')
        if ttl is not None:
            raise UnsupportedFeature('lifecycle TTL is being implemented')
        recipe = Template(template or 'coding').resolve()
        if setup:
            recipe['setup_steps'].append(setup_step(setup))
        defaults = recipe['resources']
        resources = normalize(cpu=cpu if cpu is not None else defaults.get('cpu', 1),
                              memory=memory if memory is not None else defaults.get('memory', '1GiB'),
                              gpu=gpu if gpu is not None else defaults.get('gpu', False),
                              network=network if network is not None else defaults.get('network', 'internet'))
        spec = {'template': recipe, 'resources': resources, 'runtime': runtime,
                'env': env or {}, 'mounts': mounts or [], 'name': name,
                'startup_timeout': startup_timeout, 'keep_on_error': keep_on_error}
        self._connection = connect(target)
        self.id = 'sw-' + uuid.uuid4().hex
        self._owned, self._closed, self._terminated = True, False, False
        self._target = target
        operation_id = uuid.uuid4().hex
        self._info = self._connection.call('create', identity=self.id, spec=spec, operation_id=operation_id)
        self.files = Files(self)

    @dualclassmethod
    def create(cls, **kwargs):
        return cls(**kwargs)

    @dualclassmethod
    def connect(cls, identity, *, target=None):
        self = cls.__new__(cls)
        self._connection = connect(target)
        self.id = str(identity)
        self._owned, self._closed, self._terminated = False, False, False
        self._target = target
        self._info = self._connection.call('describe', identity=self.id)
        self.files = Files(self)
        return self

    def _call(self, operation, **kwargs):
        if self._closed:
            raise RuntimeError('sandbox client is closed; connect with its ID to attach again')
        return self._connection.call(operation, identity=self.id, **kwargs)

    @property
    def spec(self):
        return copy.deepcopy(self._info['spec'])

    @property
    def timings(self):
        return dict(self._info['timings'])

    @dualmethod
    def status(self):
        self._info = self._call('describe')
        return self._info

    @dualmethod
    def exec(self, command=None, *, argv=None, cwd='/workspace', env=None, user=None,
             timeout=None, shell=None, binary=False):
        identity = uuid.uuid4().hex
        self._call('command_start', process_id=identity, command=command, argv=argv,
                    cwd=cwd, env=env, user=user, timeout=timeout, shell=shell)
        return Process(self, identity, binary=binary)

    @dualmethod
    def run(self, command=None, *, argv=None, cwd='/workspace', env=None, user=None,
            timeout=None, shell=None, check=True, binary=False):
        process = self.exec(command, argv=argv, cwd=cwd, env=env, user=user,
                            timeout=timeout, shell=shell, binary=binary)
        process.stdin.close()
        process.wait()
        result = process.result()
        if check and result.returncode:
            raise CommandError(f'command exited with {result.returncode}', result=result, operation_id=process.id)
        return result

    @dualmethod
    def setup(self, path, *, inputs=(), user='root'):
        return self._call('setup', step=setup_step(path, inputs=inputs, user=user))

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
            self._info = self._call('terminate')
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

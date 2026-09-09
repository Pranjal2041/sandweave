"""Bounded leases from an immutable baseline; every used guest is discarded."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading

from .asyncio import dualmethod
from .sandbox import Sandbox
from .snapshots import SnapshotRef
from .targets import connect


class Pool:
    def __init__(self, *, size=1, warm=0, targets=None, **sandbox_options):
        if type(size) is not int or size < 1 or type(warm) is not int or not 0 <= warm <= size:
            raise ValueError('pool size must be positive and warm must be in 0..size')
        self.size, self.warm = size, warm
        self.options = dict(sandbox_options)
        if targets is not None and 'target' in self.options:
            raise ValueError('provide target or targets, not both')
        self.targets = list(targets) if targets is not None else [self.options.pop('target', None)]
        if not self.targets:
            raise ValueError('pool requires at least one target')
        self.condition = threading.Condition()
        self.idle, self.active, self.all = deque(), set(), set()
        self.pending, self.cursor = 0, 0
        self.started, self.closed, self.failure = False, False, None
        self.executor = ThreadPoolExecutor(max_workers=size, thread_name_prefix='sandweave-refill')

    def _target(self):
        target = self.targets[self.cursor % len(self.targets)]
        self.cursor += 1
        return target

    @dualmethod
    def start(self):
        with self.condition:
            if self.closed:
                raise RuntimeError('pool is closed')
            if self.started:
                return self
            # Pin cache aliases once. Recipe preparation is performed once and
            # discarded before creating the independent episode environments.
            source = self.options.get('cache') or self.options.get('snapshot')
            if source is not None:
                connection = connect(self.targets[0])
                try:
                    source = connection.call('snapshot_spec', reference=str(source))['reference']
                finally:
                    connection.close()
                self.options.pop('snapshot', None)
                self.options['cache'] = source
            else:
                with Sandbox(target=self.targets[0], **self.options) as builder:
                    source = builder.snapshot(state='filesystem')
                # Startup services and controls survive in the saved recipe.
                self.options = {k: v for k, v in self.options.items()
                                if k not in ('template', 'setup', 'cache_key', 'refresh')}
                self.options['cache'] = source.id
            self.started = True
            self._refill()
            while len(self.idle) < self.warm and self.failure is None:
                self.condition.wait()
            if self.failure:
                error = self.failure
            else:
                return self
        self.close()
        raise error

    def _refill(self):
        while not self.closed and len(self.idle) + self.pending < self.warm and len(self.all) + self.pending < self.size:
            target = self._target()
            self.pending += 1
            self.executor.submit(self._prepare, target)

    def _prepare(self, target):
        try:
            env = Sandbox(target=target, **self.options)
        except BaseException as error:
            with self.condition:
                self.failure = error
                self.pending -= 1
                self.condition.notify_all()
            return
        with self.condition:
            self.pending -= 1
            if not self.closed:
                self.all.add(env)
                self.idle.append(env)
                self.condition.notify_all()
                return
        try:
            env.terminate()
        finally:
            env.close()

    @contextmanager
    def acquire(self):
        self.start()
        create = False
        with self.condition:
            while True:
                if self.closed:
                    raise RuntimeError('pool is closed')
                if self.failure:
                    raise self.failure
                if self.idle:
                    env = self.idle.popleft()
                    self.active.add(env)
                    self._refill()
                    break
                if len(self.all) + self.pending < self.size:
                    self.pending += 1
                    target = self._target()
                    create = True
                    break
                self.condition.wait()
        if create:
            try:
                env = Sandbox(target=target, **self.options)
            except BaseException:
                with self.condition:
                    self.pending -= 1
                    self.condition.notify_all()
                raise
            with self.condition:
                self.pending -= 1
                self.all.add(env)
                self.active.add(env)
                closed = self.closed
                self.condition.notify_all()
            if closed:
                self._release(env)
                raise RuntimeError('pool closed during checkout')
        try:
            yield env
        finally:
            self._release(env)

    def _release(self, env):
        try:
            env.terminate()
        finally:
            env.close()
            with self.condition:
                self.active.discard(env)
                self.all.discard(env)
                self._refill()
                self.condition.notify_all()

    def map(self, function, values):
        """Ordered streaming results with at most size tasks submitted at once."""
        self.start()

        def run(value):
            with self.acquire() as env:
                return function(env, value)

        source = iter(values)
        with ThreadPoolExecutor(max_workers=self.size, thread_name_prefix='sandweave-task') as executor:
            pending = deque()
            try:
                for _ in range(self.size):
                    try:
                        pending.append(executor.submit(run, next(source)))
                    except StopIteration:
                        break
                while pending:
                    result = pending.popleft().result()
                    yield result
                    try:
                        pending.append(executor.submit(run, next(source)))
                    except StopIteration:
                        pass
            finally:
                for future in pending:
                    future.cancel()

    @dualmethod
    def close(self):
        with self.condition:
            if self.closed:
                return
            self.closed = True
            self.condition.notify_all()
        self.executor.shutdown(wait=True, cancel_futures=False)
        # A pool scope owns its leases. Concurrent use after close is an error.
        errors = []
        for env in list(self.all):
            try:
                self._release(env)
            except Exception as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup('pool cleanup failed', errors)

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()

    async def __aenter__(self):
        return await self.start.aio()

    async def __aexit__(self, *args):
        await self.close.aio()

"""A benchmark supplies instructions and isolated task environments."""
from dataclasses import dataclass, field
import asyncio
import inspect
import math
from importlib import metadata
import sys
import threading

from ..sandbox.asyncio import dualmethod


async def drained(function, *args):
    """Finish a host callback before its sandbox can be released."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        # Retrieve callback errors while preserving the caller's cancellation.
        if not task.cancelled():
            task.exception()
        raise


@dataclass(frozen=True)
class TaskSpec:
    id: str
    instruction: str
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Evaluation:
    task_id: str
    score: float
    passed: bool
    feedback: str = ''


def load(name, source):
    if not isinstance(name, str):
        if source is not None:
            raise ValueError('source belongs to named benchmark integrations')
        return name
    entries = list(metadata.entry_points(group='sandweave.benchmarks.v1', name=name))
    if len(entries) > 1:
        raise ValueError('duplicate benchmark integration: ' + name)
    if name in ('osworld', 'osworld-energy50-representative'):
        if entries:
            raise ValueError('duplicate benchmark integration: ' + name)
        from .osworld import OSWorld
        return OSWorld(name, source=source)
    if entries:
        return entries[0].load()(source=source)
    raise ValueError('unknown benchmark: ' + name)


class Benchmark:
    """Lease tasks sequentially or map an agent over a bounded pool.

    An integration provides ``tasks``, ``prepare()``, ``setup(env, task)`` and
    ``evaluate(env, task)``. Preparation returns normal Pool sandbox options.
    Evaluators return Evaluation; infrastructure errors propagate unchanged.
    """
    def __init__(self, name, *, capacity=1, preload=None, source=None, **pool_options):
        if type(capacity) is not int or capacity < 1:
            raise ValueError('capacity must be a positive integer')
        preload = capacity if preload is None else preload
        if type(preload) is not int or not 0 <= preload <= capacity:
            raise ValueError('preload must be in 0..capacity')
        if {'size', 'warm'} & pool_options.keys():
            raise ValueError('use capacity and preload to size a benchmark')
        self.capacity, self.preload = capacity, preload
        self._suite = load(name, source)
        self.tasks = tuple(self._suite.tasks)
        if any(not isinstance(task, TaskSpec) or not isinstance(task.id, str) or not task.id or not isinstance(task.instruction, str)
               for task in self.tasks):
            raise ValueError('benchmark tasks must have an id and instruction')
        if len({task.id for task in self.tasks}) != len(self.tasks):
            raise ValueError('benchmark task ids must be unique')
        self._options = pool_options
        self._pool = None
        self._closed = False
        self._lock = threading.RLock()
        self._results = {}

    @property
    def results(self):
        """The latest completed evaluation for each task, in benchmark order."""
        with self._lock:
            return [self._results[task.id] for task in self.tasks if task.id in self._results]

    @dualmethod
    def start(self):
        from ..weave.pool import Pool
        with self._lock:
            if self._closed:
                raise RuntimeError('benchmark is closed')
            if self._pool is not None:
                return self
            options = {**self._suite.prepare(), **self._options}
            pool = Pool(size=self.capacity, warm=self.preload, **options)
            try:
                pool.start()
            except BaseException:
                try:
                    pool.close()
                except Exception:
                    pass  # Preserve the launch failure that caused cleanup.
                raise
            self._pool = pool
        return self

    @start.async_impl
    async def _start_async(self):
        try:
            return await drained(self.start)
        except asyncio.CancelledError:
            await self.close.aio()
            raise

    def __iter__(self):
        for spec in self.tasks:
            yield Task(self, spec)

    def task(self, identity):
        """Create a fresh attempt for a particular task."""
        for spec in self.tasks:
            if spec.id == identity:
                return Task(self, spec)
        raise KeyError(identity)

    def _evaluate(self, env, spec):
        result = self._suite.evaluate(env, spec)
        if not isinstance(result, Evaluation) or result.task_id != spec.id:
            raise ValueError('evaluator must return an Evaluation for ' + spec.id)
        if not isinstance(result.score, (int, float)) or not math.isfinite(result.score):
            raise ValueError('evaluation score must be finite')
        with self._lock:
            self._results[spec.id] = result
        return result

    @dualmethod
    def map(self, agent, *, return_exceptions=False):
        """Call agent(env, instruction), then evaluate; yield ordered results."""
        self.start()

        def run(env, spec):
            self._suite.setup(env, spec)
            agent(env, spec.instruction)
            return self._evaluate(env, spec)

        yield from self._pool.map(run, self.tasks, return_exceptions=return_exceptions)

    @map.async_impl
    async def _map_async(self, agent, *, return_exceptions=False):
        await self.start.aio()

        async def run(env, spec):
            await drained(self._suite.setup, env, spec)
            if inspect.iscoroutinefunction(agent):
                await agent(env, spec.instruction)
            else:
                value = await drained(agent, env, spec.instruction)
                if inspect.isawaitable(value):
                    await value
            return await drained(self._evaluate, env, spec)

        results = self._pool.map.aio(run, self.tasks, return_exceptions=return_exceptions)
        try:
            async for result in results:
                yield result
        finally:
            await results.aclose()

    @dualmethod
    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pool = self._pool
        try:
            if pool is not None:
                pool.close()
        finally:
            close = getattr(self._suite, 'close', None)
            if close is not None:
                close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()

    async def __aenter__(self):
        return await self.start.aio()

    async def __aexit__(self, *args):
        await drained(self.close)


class Task:
    """One attempt. Its sandbox exists only inside the task's context."""
    def __init__(self, benchmark, spec):
        self.benchmark, self.spec = benchmark, spec
        self.id, self.instruction = spec.id, spec.instruction
        self._lease, self._env = None, None
        self._entered = False
        self._lock = threading.RLock()

    def __enter__(self):
        with self._lock:
            if self._entered:
                raise RuntimeError('a task attempt can only be entered once')
            self._entered = True
            self.benchmark.start()
            lease = self.benchmark._pool.acquire()
            env = lease.__enter__()
            try:
                self.benchmark._suite.setup(env, self.spec)
            except BaseException:
                lease.__exit__(*sys.exc_info())
                raise
            self._lease, self._env = lease, env
            return env

    @dualmethod
    def evaluate(self):
        with self._lock:
            if self._env is None:
                raise RuntimeError('evaluate inside the task context')
            return self.benchmark._evaluate(self._env, self.spec)

    def __exit__(self, *args):
        with self._lock:
            lease, self._lease = self._lease, None
            self._env = None
            if lease is not None:
                return lease.__exit__(*args)

    async def __aenter__(self):
        try:
            return await drained(self.__enter__)
        except asyncio.CancelledError:
            await drained(self.__exit__, None, None, None)
            raise

    async def __aexit__(self, *args):
        return await drained(self.__exit__, *args)

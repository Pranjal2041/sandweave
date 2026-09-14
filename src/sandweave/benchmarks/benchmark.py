"""A benchmark supplies instructions and isolated task environments."""
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
import asyncio
import inspect
import heapq
import math
from importlib import metadata
import sys
import threading
import time

from ..sandbox.asyncio import dualmethod
from ..sandbox.resources import positive


async def settled(task):
    """Join a host operation even after repeated cancellation."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    return task.result()


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
    """Supply prepared tasks from a bounded pool to the client's agent loop.

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
        self._cursor = 0
        self._retry = []
        self._attempts = set()
        # Capacity waiters must not fill asyncio's shared executor and prevent
        # env.close.aio(), evaluation or unrelated SDK calls from making progress.
        self._acquisitions = ThreadPoolExecutor(max_workers=capacity,
                                               thread_name_prefix='sandweave-benchmark-acquire')

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
        # Preserve lazy, repeatable iteration for callers filtering task IDs.
        for spec in self.tasks:
            yield Task(self, spec)

    def __next__(self):
        return self.next()

    def _put_back(self, task):
        with self._lock:
            if task._index is not None:
                heapq.heappush(self._retry, task._index)
                task._index = None

    def _next(self, timeout, cancelled, holder, queued_at=None):
        if timeout is not None:
            positive(timeout, 'timeout')
            if queued_at is not None:
                timeout -= time.monotonic() - queued_at
                if timeout <= 0:
                    raise TimeoutError('benchmark checkout is waiting for capacity')
        with self._lock:
            if self._closed:
                raise RuntimeError('benchmark is closed')
            if self._retry:
                index = heapq.heappop(self._retry)
            elif self._cursor < len(self.tasks):
                index = self._cursor
                self._cursor += 1
            else:
                return None
            task = Task(self, self.tasks[index])
            task._index = index
            holder.append(task)
        try:
            if cancelled.is_set():
                task._cancel()
            task._acquire(timeout=timeout)
            if cancelled.is_set():
                raise InterruptedError('benchmark checkout cancelled')
            return task
        except BaseException:
            task.close()
            if not task._setup_started or cancelled.is_set():
                self._put_back(task)
            raise

    @dualmethod
    def next(self, *, timeout=None):
        """Acquire and prepare the next task, waiting for a free pool lease.

        Timeout limits pool checkout, not initial preparation or task setup.
        A timed-out checkout does not consume the task. Evaluation is explicit.
        """
        task = self._next(timeout, threading.Event(), [])
        if task is None:
            raise StopIteration
        return task

    @next.async_impl
    async def _next_async(self, *, timeout=None):
        if timeout is not None:
            positive(timeout, 'timeout')
        cancelled, holder = threading.Event(), []
        future = self._acquisitions.submit(self._next, timeout, cancelled, holder, time.monotonic())
        pending = asyncio.wrap_future(future)
        expired = False
        def expire_queued():
            nonlocal expired
            expired = future.cancel()
        timer = asyncio.get_running_loop().call_later(timeout, expire_queued) if timeout is not None else None
        try:
            task = await asyncio.shield(pending)
        except asyncio.CancelledError:
            if expired:
                raise TimeoutError('benchmark checkout is waiting for capacity') from None
            cancelled.set()
            if future.cancel():
                raise
            if holder:
                holder[0]._cancel()
            try:
                await settled(pending)
            except Exception:
                pass
            if holder:
                await drained(holder[0].close)
                self._put_back(holder[0])
            raise
        finally:
            if timer is not None:
                timer.cancel()
        if task is None:
            raise StopAsyncIteration
        return task

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
            attempts = tuple(self._attempts)
        for task in attempts:
            task._cancel()
        errors = []
        try:
            self._acquisitions.shutdown(wait=True, cancel_futures=True)
            for task in attempts:
                try:
                    task.close()
                except Exception as error:
                    errors.append(error)
            if pool is not None:
                pool.close()
        finally:
            close = getattr(self._suite, 'close', None)
            if close is not None:
                close()
        if errors:
            raise ExceptionGroup('benchmark task cleanup failed', errors)

    @close.async_impl
    async def _close_async(self):
        await drained(self.close)

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()

    async def __aenter__(self):
        return await self.start.aio()

    async def __aexit__(self, *args):
        await drained(self.close)


class Task:
    """One owned attempt; close it explicitly or use its context manager."""
    def __init__(self, benchmark, spec):
        self.benchmark, self.spec = benchmark, spec
        self.id, self.instruction = spec.id, spec.instruction
        self._lease, self._env = None, None
        self._entered = False
        self._closed = False
        self._setup_started = False
        self._lease_entered = False
        self._index = None
        self._cancelled = threading.Event()
        self._lock = threading.RLock()

    @property
    def env(self):
        """The prepared sandbox returned by next(); no implicit acquisition."""
        with self._lock:
            if self._env is None:
                raise RuntimeError('task has no active sandbox')
            return self._env

    def _cancel(self):
        self._cancelled.set()
        lease = self._lease
        if lease is not None:
            lease.cancelled.set()

    def _acquire(self, *, timeout=None):
        with self._lock:
            if self._closed or self._cancelled.is_set():
                raise RuntimeError('task is closed')
            if self._env is not None:
                return self._env
            self.benchmark.start()
            with self.benchmark._lock:
                if self.benchmark._closed:
                    raise RuntimeError('benchmark is closed')
                self.benchmark._attempts.add(self)
            try:
                lease = self.benchmark._pool.acquire(timeout=timeout)
                self._lease = lease
                if self._cancelled.is_set():
                    lease.cancelled.set()
                env = lease.__enter__()
                self._lease_entered = True
                self._env = env
                env._lease_close = self.close
                if self._cancelled.is_set():
                    raise InterruptedError('benchmark checkout cancelled')
                self._setup_started = True
                self.benchmark._suite.setup(env, self.spec)
            except BaseException:
                self.__exit__(*sys.exc_info())
                raise
            if self._closed:
                raise RuntimeError('task was closed during setup')
            return env

    def __enter__(self):
        with self._lock:
            if self._entered:
                raise RuntimeError('a task attempt can only be entered once')
            self._entered = True
            return self._acquire()

    @dualmethod
    def evaluate(self):
        with self._lock:
            if self._env is None:
                raise RuntimeError('evaluate while the task has an active sandbox')
            return self.benchmark._evaluate(self._env, self.spec)

    def __exit__(self, *args):
        self._cancel()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            lease, self._lease = self._lease, None
            env, self._env = self._env, None
            try:
                if lease is not None and self._lease_entered:
                    return lease.__exit__(*args)
            finally:
                try:
                    if env is not None:
                        env._close_connection()
                finally:
                    with self.benchmark._lock:
                        self.benchmark._attempts.discard(self)

    @dualmethod
    def close(self):
        """Release the sandbox and its capacity once; safe to repeat."""
        self.__exit__(None, None, None)

    @close.async_impl
    async def _close_async(self):
        await drained(self.close)

    async def __aenter__(self):
        pending = asyncio.create_task(asyncio.to_thread(self.__enter__))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            self._cancel()
            try:
                await settled(pending)
            except Exception:
                pass
            await drained(self.close)
            raise

    async def __aexit__(self, *args):
        return await drained(self.__exit__, *args)

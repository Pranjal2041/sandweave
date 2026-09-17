"""Suspend Harbor at its agent boundary while the caller owns a task lease."""
import asyncio
import copy
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
import threading
import time
import inspect

from harbor.agents.base import BaseAgent
from harbor.agents.capabilities import AgentCapabilities
from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, TrialConfig
from harbor.trial.hooks import TrialEvent
from harbor.trial.trial import Trial

from ..benchmark import Evaluation, drained, settled
from ...sandbox.asyncio import dualmethod
from ...sandbox.pool import Lease
from ...sandbox.workspace import home

current_session = ContextVar('sandweave_harbor_session', default=None)


class Loop:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name='sandweave-harbor', daemon=True)
        self.thread.start()
        self.closed = False

    def call(self, coroutine):
        if self.closed:
            coroutine.close()
            raise RuntimeError('benchmark is closed')
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result()

    def close(self):
        if self.closed:
            return
        self.call(self.loop.shutdown_default_executor())
        self.closed = True
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join()
        self.loop.close()


@dataclass
class Phase:
    instruction: str
    begin: asyncio.Event = field(default_factory=asyncio.Event)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    finish: asyncio.Event = field(default_factory=asyncio.Event)
    evaluation: Evaluation | None = None


@dataclass(frozen=True)
class AgentInputs:
    """Harbor-provided agent inputs and the native result context for this phase."""
    mcp_servers: tuple
    skills_dir: str | None
    context: object
    logs_dir: str
    trajectory: Path | None = None
    resume: bool = False
    previous_context: object = None


class PullAgent(BaseAgent):
    # The caller is the agent. Preserve native/ATIF inputs and continuation
    # state at the pull boundary instead of rejecting trajectory-bearing tasks.
    capabilities = AgentCapabilities(resume=True, load_native_trajectory=True, load_atif_trajectory=True)

    @staticmethod
    def name():
        return 'sandweave-client'

    def version(self):
        from importlib.metadata import version
        return version('sandweave')

    async def setup(self, environment):
        pass

    async def run(self, instruction, environment, context):
        await self._pull(instruction, environment, context)

    async def load(self, instruction, environment, context):
        await self._pull(instruction, environment, context, trajectory=self.load_trajectory)

    async def resume(self, instruction, environment, context):
        await self._pull(instruction, environment, context, resume=True)

    async def _pull(self, instruction, environment, context, *, trajectory=None, resume=False):
        session = current_session.get()
        if session is None:
            raise RuntimeError('PullAgent requires a Sandweave benchmark')
        phase = session.phases[-1]
        if session.env is None:
            session.env = environment.agent_view()
        session.env.defaults = dict(user=environment._resolve_user(None),
                                    cwd=environment.task_env_config.workdir,
                                    env=dict(environment._persistent_env))
        session.env.scoped_env = {key: value for overlay in environment._exec_env_overlays.get()
                                 for key, value in overlay.items()}
        session.env._task_instruction = instruction
        phase.instruction = instruction
        previous = getattr(session.env, 'harbor', None)
        session.env.harbor = AgentInputs(tuple(self.mcp_servers), self.skills_dir,
            context, str(self.environment_logs_dir), trajectory=trajectory, resume=resume,
            previous_context=previous.context if previous is not None and resume else None)
        phase.started.set()
        await phase.finish.wait()


class Session:
    def __init__(self, pool, spec):
        self.pool, self.spec = pool, spec
        self.trial = self.env = None
        self.environments = set()
        self.phases = []
        self.index = 0
        self.error = None
        self.changed = asyncio.Condition()
        self.run = asyncio.create_task(self._run())

    async def _boundary(self, event):
        definition = self.trial.task
        index = len(self.phases)
        if index:
            from harbor.models.task.verifier_mode import resolve_step_verifier_mode, VerifierEnvironmentMode
            previous = definition.config.steps[index - 1]
            if resolve_step_verifier_mode(definition.config, previous) == VerifierEnvironmentMode.SHARED:
                # Harbor 0.23 leaves the previous step's injected grading files
                # in a shared guest. Clear only Harbor's reserved grading paths.
                await self.trial.agent_environment.empty_dirs([
                    self.trial.agent_env_paths.tests_dir, self.trial.agent_env_paths.verifier_dir], chmod=True)
        instruction = (definition.step_instruction(definition.config.steps[index].name)
                       if definition.has_steps else definition.instruction)
        phase = Phase(instruction)
        async with self.changed:
            self.phases.append(phase)
            self.changed.notify_all()
        # Harbor emits AGENT_START before starting the agent's timeout clock.
        # Warm tasks and clients between steps therefore consume no agent time.
        await phase.begin.wait()

    async def _run(self):
        token = current_session.set(self)
        try:
            options = copy.deepcopy(self.pool.trial_options)
            agent = options.pop('agent', {})
            environment = options.pop('environment', {})
            config = TrialConfig(
                task=self.pool.suite.task_configs[self.spec.id].model_copy(deep=True),
                agent=AgentConfig(**{**agent, 'name': None, 'import_path': __name__ + ':PullAgent'}),
                environment=EnvironmentConfig(**{**environment, 'type': None,
                    'import_path': 'sandweave.benchmarks.harbor.provider:SandweaveEnvironment'}),
                trials_dir=self.pool.output,
                **options,
            )
            self.trial = await Trial.create(config)
            self.trial.add_hook(TrialEvent.AGENT_START, self._boundary)
            result = await self.trial.run()
            if result.exception_info:
                detail = result.exception_info
                raise RuntimeError(f'{detail.exception_type}: {detail.exception_message}; logs: {self.trial.paths.trial_dir}')
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.error = error
        finally:
            current_session.reset(token)
            async with self.changed:
                self.changed.notify_all()

    async def phase(self, index):
        async with self.changed:
            while len(self.phases) <= index and not self.run.done():
                # Completion notification is sent in _run's finally, before
                # Task.done() changes; a done callback also wakes the waiters.
                await self.changed.wait()
            if self.error:
                raise self.error
            return self.phases[index] if len(self.phases) > index else None

    async def begin(self):
        phase = await self.phase(self.index)
        if phase is None:
            raise RuntimeError('Harbor trial ended before its agent phase')
        phase.begin.set()
        waiter = asyncio.create_task(phase.started.wait())
        try:
            await asyncio.wait((waiter, self.run), return_when=asyncio.FIRST_COMPLETED)
            if not phase.started.is_set():
                if self.error:
                    raise self.error
                raise RuntimeError('Harbor trial ended before checkout')
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        return self.env

    async def evaluate(self):
        phase = self.phases[self.index]
        if phase.evaluation is not None:
            return phase.evaluation
        phase.finish.set()
        await self.phase(self.index + 1)
        result = self.trial.result
        # MultiStepTrial records failures on the step, not necessarily on the
        # trial. Aggregating earlier rewards must not hide a failed last step.
        for step in (result.step_results or [])[self.index:]:
            if step.exception_info:
                detail = step.exception_info
                raise RuntimeError(f'{detail.exception_type}: {detail.exception_message}; '
                                   f'logs: {self.trial.paths.trial_dir}')
        if result.step_results and not self.run.done():
            result = result.step_results[self.index]
        if result.exception_info:
            raise RuntimeError(result.exception_info.exception_message)
        rewards = result.verifier_result.rewards if result.verifier_result else None
        skipped = self.trial.config.verifier.disable
        if rewards is None and not skipped:
            raise RuntimeError('Harbor verifier returned no rewards')
        phase.evaluation = Evaluation(self.spec.id, rewards=dict(rewards or {}), skipped=skipped,
                                      feedback=str(self.trial.paths.trial_dir))
        return phase.evaluation

    async def next_step(self):
        if self.phases[self.index].evaluation is None:
            raise RuntimeError('evaluate the current step before requesting the next one')
        phase = await self.phase(self.index + 1)
        if phase is None:
            return None
        self.index += 1
        await self.begin()
        return phase.instruction

    async def close(self):
        if not self.run.done():
            self.run.cancel()
        await asyncio.gather(self.run, return_exceptions=True)
        cleanup = await asyncio.gather(*(environment.stop(delete=True) for environment in self.environments),
                                       return_exceptions=True)
        if self.env is not None:
            self.env._close_connection()
        failures = [error for error in cleanup if isinstance(error, Exception)]
        if failures:
            raise ExceptionGroup('Harbor environment cleanup failed', failures)


class TaskPool:
    def __init__(self, suite, *, capacity, preload, output=None, harbor=None, **options):
        self.trial_options = copy.deepcopy(harbor or {})
        reserved = {'task', 'trials_dir', 'install_only', 'source_trial'} & self.trial_options.keys()
        if reserved:
            raise ValueError('The benchmark pull interface owns these Harbor settings: ' + ', '.join(sorted(reserved)))
        conflicts = {'image', 'template', 'setup', 'cache', 'snapshot', 'cache_key', 'env', 'mounts', 'network', 'runtime'} & options.keys()
        if conflicts:
            raise ValueError('Harbor task definitions own these settings: ' + ', '.join(sorted(conflicts)))
        self.suite, self.capacity, self.preload = suite, capacity, preload
        self.options = options
        self.output = Path(output) if output is not None else home() / 'benchmarks' / 'harbor' / 'trials'
        self.sessions = {}
        self.warm = {}
        self.live = set()
        self.prepared = {}
        self.images = {}
        self.builds = {}
        # Capacity/launch waits must never occupy the executor used by command
        # transfers and cleanup. The number of launch waits is capacity-bounded.
        self.launches = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix='sandweave-harbor-launch')
        self.seen = set()
        self.closed = False
        self.condition = asyncio.Condition()

    def start(self):
        self.suite.loop.call(self._start())
        return self

    def _session(self, spec):
        session = Session(self, spec)
        self.seen.add(spec.id)
        self.live.add(session)
        async def notify():
            async with session.changed:
                session.changed.notify_all()
        session.run.add_done_callback(lambda _: asyncio.create_task(notify()))
        return session

    async def _start(self):
        for spec in self.suite.tasks[:self.preload]:
            self.warm[spec.id] = self._session(spec)
        await asyncio.gather(*(session.phase(0) for session in self.warm.values()))

    def _refill(self):
        if self.closed:
            return
        for spec in self.suite.tasks:
            if len(self.warm) >= self.preload or len(self.live) >= self.capacity:
                break
            if spec.id not in self.seen:
                self.warm[spec.id] = self._session(spec)

    async def environment_pool(self, key, options):
        from ...weave.pool import Pool
        if key not in self.prepared:
            async def prepare():
                pool = Pool(size=self.capacity, warm=0, **{**options, **self.options})
                try:
                    await self.blocking(pool.start)
                except BaseException:
                    await pool.close.aio()
                    raise
                return pool
            self.prepared[key] = asyncio.create_task(prepare())
        pending = self.prepared[key]
        try:
            return await asyncio.shield(pending)
        except Exception:
            if self.prepared.get(key) is pending:
                self.prepared.pop(key)
            raise

    async def blocking(self, function):
        pending = asyncio.get_running_loop().run_in_executor(self.launches, function)
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            try:
                await settled(pending)
            except Exception:
                pass
            raise

    async def pin(self, image):
        if image not in self.images:
            async def resolve():
                from ...templates.registry import Registry
                registry = Registry(image, home() / 'images' / 'blobs')
                resolved = await self.blocking(registry.resolve)
                return 'docker://' + registry.host + '/' + registry.repository + '@' + resolved['digest']
            self.images[image] = asyncio.create_task(resolve())
        pending = self.images[image]
        try:
            return await asyncio.shield(pending)
        except Exception:
            if self.images.get(image) is pending:
                self.images.pop(image)
            raise

    async def build(self, directory, settings):
        from ...templates.build import build, remote_context
        from ...templates.resolve import fingerprint
        key = fingerprint({'directory': str(directory) if remote_context(directory) else str(Path(directory).resolve()), **settings})
        if key not in self.builds:
            self.builds[key] = asyncio.create_task(self.blocking(lambda: build(directory,
                target=self.options.get('target'), log=self.output / 'builds' / (key + '.log'), **settings)))
        pending = self.builds[key]
        try:
            return await asyncio.shield(pending)
        except Exception:
            if self.builds.get(key) is pending:
                self.builds.pop(key)
            raise

    def acquire_task(self, spec, *, timeout=None):
        cancelled = threading.Event()
        return Lease(self._acquire(spec, cancelled, timeout), cancelled)

    @contextmanager
    def _acquire(self, spec, cancelled, timeout):
        session = self.suite.loop.call(self._checkout(spec, cancelled, timeout))
        try:
            yield session.env
        finally:
            self.suite.loop.call(self._release(session))

    async def _checkout(self, spec, cancelled, timeout):
        deadline = None if timeout is None else time.monotonic() + timeout
        session = None
        while session is None:
            unused = None
            async with self.condition:
                if self.closed or cancelled.is_set():
                    raise InterruptedError('Harbor checkout cancelled')
                session = self.warm.pop(spec.id, None)
                if session is None and len(self.live) < self.capacity:
                    session = self._session(spec)
                if session is None:
                    if self.warm:
                        _, unused = self.warm.popitem()
                    else:
                        remaining = None if deadline is None else deadline - time.monotonic()
                        if remaining is not None and remaining <= 0:
                            raise TimeoutError('benchmark checkout is waiting for capacity')
                        try:
                            await asyncio.wait_for(self.condition.wait(), min(.1, remaining) if remaining is not None else .1)
                        except asyncio.TimeoutError:
                            pass
            if unused is not None:
                # Keep its reservation until teardown completes, but never
                # hold the capacity condition across a worker/network wait.
                # Other leases must still be able to release or check out.
                await unused.close()
                async with self.condition:
                    self.live.discard(unused)
                    self.condition.notify_all()
        try:
            pending = asyncio.create_task(session.begin())
            while not pending.done():
                if cancelled.is_set():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                    raise InterruptedError('Harbor checkout cancelled')
                await asyncio.wait((pending,), timeout=.1)
            await pending
            if cancelled.is_set():
                raise InterruptedError('Harbor checkout cancelled')
            self.sessions[session.env.id] = session
            self._refill()
            return session
        except BaseException:
            await self._release(session)
            raise

    async def _release(self, session):
        await session.close()
        async with self.condition:
            if session.env is not None:
                self.sessions.pop(session.env.id, None)
            self.live.discard(session)
            self._refill()
            self.condition.notify_all()

    @dualmethod
    def map(self, function, values, *, return_exceptions=False):
        def run(spec):
            with self.acquire_task(spec) as env:
                while True:
                    result = function(env, replace(spec, instruction=env._task_instruction))
                    if self.suite.next_step(env, spec) is None:
                        return result
        source, pending = iter(values), deque()
        with ThreadPoolExecutor(max_workers=self.capacity, thread_name_prefix='sandweave-harbor-map') as executor:
            try:
                for _ in range(self.capacity):
                    try:
                        pending.append(executor.submit(run, next(source)))
                    except StopIteration:
                        break
                while pending:
                    try:
                        result = pending.popleft().result()
                    except Exception as error:
                        if not return_exceptions:
                            raise
                        result = error
                    yield result
                    try:
                        pending.append(executor.submit(run, next(source)))
                    except StopIteration:
                        pass
            finally:
                for future in pending:
                    future.cancel()

    @map.async_impl
    async def _map_async(self, function, values, *, return_exceptions=False):
        async def run(spec):
            async with self.acquire_task(spec) as env:
                while True:
                    current = replace(spec, instruction=env._task_instruction)
                    if inspect.iscoroutinefunction(function):
                        result = await function(env, current)
                    else:
                        result = await drained(function, env, current)
                    if await drained(self.suite.next_step, env, spec) is None:
                        return result
        source, pending = iter(values), deque()
        try:
            for _ in range(self.capacity):
                try:
                    pending.append(asyncio.create_task(run(next(source))))
                except StopIteration:
                    break
            while pending:
                try:
                    result = await pending.popleft()
                except Exception as error:
                    if not return_exceptions:
                        raise
                    result = error
                yield result
                try:
                    pending.append(asyncio.create_task(run(next(source))))
                except StopIteration:
                    pass
        finally:
            for pending_task in pending:
                pending_task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def close(self):
        self.suite.loop.call(self._close())

    async def _close(self):
        async with self.condition:
            self.closed = True
            self.condition.notify_all()
        results = await asyncio.gather(*(session.close() for session in self.live), return_exceptions=True)
        self.live.clear()
        self.sessions.clear()
        self.warm.clear()
        prepared = await asyncio.gather(*self.prepared.values(), return_exceptions=True)
        results += await asyncio.gather(*(pool.close.aio() for pool in prepared if not isinstance(pool, BaseException)),
                                        return_exceptions=True)
        await asyncio.gather(*self.images.values(), return_exceptions=True)
        builds = await asyncio.gather(*self.builds.values(), return_exceptions=True)
        for snapshot in builds:
            if not isinstance(snapshot, BaseException):
                snapshot._connection.close()
        self.launches.shutdown(wait=True)
        errors = [error for error in results if isinstance(error, Exception)]
        if errors:
            raise ExceptionGroup('Harbor pool cleanup failed', errors)

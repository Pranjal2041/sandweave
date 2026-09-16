"""Provider conformance derived from Harbor's API, independent of datasets."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip('harbor')
from harbor.models.task.config import EnvironmentConfig
from sandweave.benchmarks.harbor.provider import AgentSandbox
from test_harbor import provider


def test_sidecar_exec_is_posix_and_does_not_inherit_main_scopes(tmp_path):
    async def run():
        env = provider(tmp_path, persistent_env={'MAIN': 'private'})
        execute = AsyncMock(return_value=SimpleNamespace(
            stdin=SimpleNamespace(close=SimpleNamespace(aio=AsyncMock())),
            wait=SimpleNamespace(aio=AsyncMock()),
            poll=SimpleNamespace(aio=AsyncMock(return_value=0)),
            stdout=SimpleNamespace(read=SimpleNamespace(aio=AsyncMock(return_value='ok'))),
            stderr=SimpleNamespace(read=SimpleNamespace(aio=AsyncMock(return_value='')))))
        sidecar = SimpleNamespace(exec=SimpleNamespace(aio=execute))
        env.project = SimpleNamespace(views={'sidecar': sidecar})
        with env.with_default_user('agent'), env.scoped_exec_env({'SCOPED': 'private'}):
            result = await env.service_exec('printf ok', service='sidecar', env={'CALL': 'visible'})
        assert result.return_code == 0
        arguments = execute.call_args.kwargs
        assert arguments['shell'] == '/bin/sh'
        assert arguments['user'] is None and arguments['cwd'] is None
        assert arguments['env'] == {'CALL': 'visible'}
    asyncio.run(run())


def test_pull_commands_preserve_harbor_scoped_environment_precedence():
    env = AgentSandbox.__new__(AgentSandbox)
    env.defaults = {'env': {'A': 'persistent', 'B': 'persistent'}}
    env.scoped_env = {'A': 'agent'}
    assert env._defaults({'env': {'A': 'call', 'B': 'call'}})['env'] == {'A': 'agent', 'B': 'call'}


@pytest.mark.parametrize('extra', [False, True])
def test_compose_task_environment_is_startup_state_not_exec_overlay(tmp_path, extra):
    directory = tmp_path / 'environment'
    directory.mkdir()
    compose = (tmp_path / 'extra.yaml') if extra else directory / 'docker-compose.yaml'
    compose.write_text('services: {main: {image: ubuntu:22.04}}')
    env = provider(tmp_path, task_env_config=EnvironmentConfig(
        docker_image='ubuntu:22.04', env={'TASK': 'startup'}),
        persistent_env={'TRIAL': 'persistent'}, extra_docker_compose=[compose] if extra else None)
    assert env._startup_env() == {'TASK': 'startup', 'TRIAL': 'persistent'}
    assert env._merge_env(None) == {'TRIAL': 'persistent'}


def test_slow_warm_eviction_does_not_hold_other_lease_releases(tmp_path):
    import threading
    from sandweave.benchmarks.harbor.runner import TaskPool
    async def run():
        started, finish = asyncio.Event(), asyncio.Event()
        class Session:
            env = None
            def __init__(self, slow=False):
                self.slow = slow
            async def close(self):
                if self.slow:
                    started.set()
                    await finish.wait()
        pool = TaskPool(SimpleNamespace(tasks=[]), capacity=2, preload=1, output=tmp_path)
        warm, active = Session(True), Session()
        pool.warm['warm'] = warm
        pool.live.update((warm, active))
        cancelled = threading.Event()
        pending = asyncio.create_task(pool._checkout(SimpleNamespace(id='requested'), cancelled, None))
        try:
            await asyncio.wait_for(started.wait(), 2)
            assert warm in pool.live  # Teardown still owns its reservation.
            await asyncio.wait_for(pool._release(active), 2)
            assert pool.live == {warm}
            cancelled.set()
            finish.set()
            with pytest.raises(InterruptedError, match='cancelled'):
                await pending
            assert not pool.live
        finally:
            cancelled.set()
            finish.set()
            await asyncio.gather(pending, return_exceptions=True)
            pool.launches.shutdown()
    asyncio.run(run())

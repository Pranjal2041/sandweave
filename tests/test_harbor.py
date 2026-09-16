"""Harbor contracts; these do not require a worker or download task images."""
import asyncio
from pathlib import Path

import pytest

pytest.importorskip('harbor')

from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
from harbor.models.trial.config import ResourceMode

from sandweave.benchmarks.harbor.provider import SandweaveEnvironment


def provider(tmp_path, **overrides):
    config = overrides.pop('task_env_config', EnvironmentConfig(docker_image='ubuntu:22.04'))
    return SandweaveEnvironment(environment_dir=tmp_path / 'environment',
        environment_name='test', session_id='test', trial_paths=TrialPaths(trial_dir=tmp_path / 'trial'),
        task_env_config=config, **overrides)


def test_provider_preserves_settings_and_declares_actual_resource_modes(tmp_path):
    env = provider(tmp_path, task_env_config=EnvironmentConfig(
        docker_image='ubuntu:22.04', workdir='/work', cpus=3, memory_mb=4096,
        env={'TASK_SETTING': 'present'}))
    options = env.options()
    assert options['cpu'].vcpus == 3 and options['cpu'].quota is None
    assert options['memory'].guest == '4096MiB'
    assert options['template']['workdir'] == '/work'
    assert options['template']['command_shell'] == '/bin/bash'
    assert options['env'] == {'TASK_SETTING': 'present'}
    with pytest.raises(ValueError):
        provider(tmp_path, cpu_enforcement_policy=ResourceMode.LIMIT)


def test_provider_does_not_silently_replace_unsupported_environments(tmp_path):
    with pytest.raises(ValueError, match='prebuilt'):
        provider(tmp_path, task_env_config=EnvironmentConfig())
    directory = tmp_path / 'environment'
    directory.mkdir()
    (directory / 'docker-compose.yaml').write_text('services: {}')
    with pytest.raises(ValueError, match='Compose'):
        provider(tmp_path)


def test_named_rewards_do_not_invent_score_or_pass_threshold():
    from sandweave.benchmarks import Evaluation
    result = Evaluation('task', rewards={'accuracy': .5, 'cost': 7})
    assert result.score is None and result.passed is None
    assert result.rewards == {'accuracy': .5, 'cost': 7}


def test_agent_handle_preserves_phase_defaults_when_run_passes_none():
    from sandweave.benchmarks.harbor.provider import AgentSandbox
    env = AgentSandbox.__new__(AgentSandbox)
    env.defaults = {'user': 'nobody', 'cwd': '/work', 'env': {'A': 'phase', 'B': 'base'}}
    options = env._defaults({'user': None, 'cwd': None, 'env': {'B': 'call'}})
    assert options == {'user': 'nobody', 'cwd': '/work', 'env': {'A': 'phase', 'B': 'call'}}


def test_network_policy_cannot_be_silently_weakened(tmp_path):
    from harbor.models.task.config import NetworkPolicy
    with pytest.raises(ValueError, match='allowlist'):
        provider(tmp_path, network_policy=NetworkPolicy(network_mode='allowlist', allowed_hosts=['example.com']))
    env = provider(tmp_path, network_policy=NetworkPolicy(network_mode='no-network'))
    assert env.options()['network'].mode == 'offline'


def make_task(root, name='task', *, image='ubuntu:22.04', steps=False):
    task = root / name
    task.mkdir(parents=True)
    (task / 'environment').mkdir()
    (task / 'task.toml').write_text('''version = "1.0"
[environment]
docker_image = "''' + image + '''"
cpus = 1
memory_mb = 256
[agent]
timeout_sec = 60
[verifier]
timeout_sec = 60
''' + ('''
[[steps]]
name = "first"
[[steps]]
name = "second"
''' if steps else ''))
    for index, directory in enumerate((task / 'steps' / 'first', task / 'steps' / 'second') if steps else (task,)):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'instruction.md').write_text('Write ' + str(index + 1) + ' into /answer')
        tests = directory / 'tests'
        tests.mkdir()
        (tests / 'test.sh').write_text('''#!/bin/bash
mkdir -p /logs/verifier
if [ "$(cat /answer 2>/dev/null)" = "''' + str(index + 1) + '''" ]; then
  printf '{"correct": 1, "cost": 0.25}' > /logs/verifier/reward.json
else
  printf '{"correct": 0, "cost": 0.25}' > /logs/verifier/reward.json
fi
''')
    return task


def test_local_discovery_and_close_without_start(tmp_path):
    from sandweave import Benchmark
    make_task(tmp_path, 'a')
    make_task(tmp_path, 'b', steps=True)
    bench = Benchmark('harbor', source=tmp_path, capacity=2)
    assert [task.id for task in bench.tasks] == ['a', 'b']
    bench.close()
    assert not bench._suite.loop.thread.is_alive()


def test_failed_image_resolution_can_retry_without_losing_deduplication(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from sandweave.benchmarks.harbor import runner
    calls = []
    def resolve(registry):
        calls.append(registry.version)
        if len(calls) == 1:
            raise OSError('temporary registry failure')
        return {'digest': 'sha256:' + 'a' * 64}
    monkeypatch.setattr('sandweave.templates.registry.Registry.resolve', resolve)
    monkeypatch.setattr(runner, 'home', lambda: tmp_path)
    async def run():
        pool = runner.TaskPool(SimpleNamespace(tasks=[]), capacity=2, preload=0)
        try:
            with pytest.raises(OSError, match='temporary'):
                await pool.pin('docker://ubuntu:22.04')
            results = await asyncio.gather(*(pool.pin('docker://ubuntu:22.04') for _ in range(64)))
            assert len(set(results)) == 1
            assert len(calls) == 2
        finally:
            await pool._close()
    asyncio.run(run())

"""Cross-feature Harbor contracts, exercised through Benchmark's pull protocol."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path

import pytest

pytest.importorskip('harbor')
from sandweave import Benchmark, Memory, Sandbox
from test_harbor import make_task

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_HARBOR_INTEGRATION'), reason='explicit disposable worker required')]


def test_minimal_sidecar_shell_collection_and_scoped_environment(tmp_path):
    path = make_task(tmp_path)
    (path / 'environment/docker-compose.yaml').write_text('''services:
  sidecar:
    image: busybox:1.37.0
    command: [sleep, infinity]
    environment:
      ROLE: sidecar
''')
    config = path / 'task.toml'
    config.write_text(config.read_text().replace('version = "1.0"', '''version = "1.0"
artifacts = [{source = "/evidence", service = "sidecar", destination = "sidecar-evidence"}]
''').replace('[verifier]\n', '''[verifier]
collect = [{service = "sidecar", command = "printf $ROLE > /evidence"}]
'''))
    bench = Benchmark('harbor', source=path, memory=Memory('256MiB', '256MiB'),
        harbor={'agent': {'env': {'ROLE': 'main-agent'}}})
    try:
        task = bench.next()
        assert task.env.run('printf "$ROLE"', env={'ROLE': 'call'}).stdout == 'main-agent'
        provider = bench._pool.sessions[task.env.id].trial.agent_environment
        async def check():
            with provider.scoped_exec_env({'ROLE': 'main-scope'}):
                return await provider.service_exec('printf "$ROLE"; test ! -e /bin/bash', service='sidecar')
        result = bench._suite.loop.call(check())
        assert (result.return_code, result.stdout) == (0, 'sidecar')
        task.env.run('echo 1 > /answer', check=True)
        result = task.evaluate()
        assert result.rewards['correct'] == 1
        assert (Path(result.feedback) / 'artifacts/sidecar-evidence').read_text() == 'sidecar'
        task.close()
    finally:
        bench.close()


def test_same_image_intermediate_verifiers_do_not_wait_for_agent_capacity(tmp_path):
    for index in range(4):
        path = make_task(tmp_path, name=str(index), steps=True)
        config = path / 'task.toml'
        config.write_text(config.read_text().replace('version = "1.0"', '''version = "1.0"
artifacts = ["/answer"]
''').replace('[environment]\n', '[environment]\nworkdir = "/tests"\n')
            .replace('[verifier]\n', '[verifier]\nenvironment_mode = "separate"\n'))
    bench = Benchmark('harbor', source=tmp_path, capacity=4, memory=Memory('256MiB', '256MiB'))
    try:
        tasks = [bench.next() for _ in range(4)]
        def solve(task):
            identity = task.env.id
            task.env.run('echo 1 > /answer; echo private > /state', check=True)
            assert task.evaluate().rewards['correct'] == 1
            task.next_step()
            assert task.env.id == identity
            assert task.env.run('cat /state', check=True).stdout == 'private\n'
            task.env.run('echo 2 > /answer', check=True)
            assert task.evaluate().rewards['correct'] == 1
            task.close()
        with ThreadPoolExecutor(4) as executor:
            futures = [executor.submit(solve, task) for task in tasks]
            with Sandbox(memory=Memory('256MiB', '256MiB')) as other:
                assert other.run('echo responsive').stdout == 'responsive\n'
            for future in futures:
                future.result(timeout=120)
        assert {role for role, key in bench._pool.prepared} == {'agent', 'verifier'}
    finally:
        bench.close()


def test_health_probe_timeout_retries_without_aborting_startup(tmp_path):
    path = make_task(tmp_path)
    (path / 'environment/docker-compose.yaml').write_text('''services:
  main:
    healthcheck:
      test: [CMD-SHELL, "if test -e /probe; then exit 0; else touch /probe; sleep 10; fi"]
      timeout: 100ms
      interval: 100ms
      retries: 3
''')
    bench = Benchmark('harbor', source=path, memory=Memory('256MiB', '256MiB'))
    try:
        task = bench.next()
        assert task.env.run('test -e /probe', check=True).returncode == 0
        task.close()
    finally:
        bench.close()


def test_native_resume_and_task_trajectory_are_delivered_to_the_client(tmp_path):
    path = make_task(tmp_path, steps=True)
    trajectory = path / 'steps/first/trajectory.json'
    document = {'schema_version': 'ATIF-v1.6', 'session_id': 'prior-session',
        'agent': {'name': 'fixture', 'version': '1'},
        'steps': [{'step_id': 1, 'timestamp': '2026-01-01T00:00:00Z', 'source': 'user',
                   'message': 'Earlier task context.'}]}
    trajectory.write_text(json.dumps(document))
    bench = Benchmark('harbor', source=path, memory=Memory('256MiB', '256MiB'),
                      harbor={'agent': {'resume_trajectory': True}})
    try:
        task = bench.next()
        assert json.loads(task.env.harbor.trajectory.read_text()) == document
        assert not task.env.harbor.resume
        context = task.env.harbor.context
        context.metadata = {'client_state': 'retained'}
        task.env.run('echo 1 > /answer', check=True)
        assert task.evaluate().rewards['correct'] == 1
        task.next_step()
        assert task.env.harbor.resume
        assert task.env.harbor.previous_context is context
        assert task.env.harbor.trajectory is None
        task.env.run('echo 2 > /answer', check=True)
        assert task.evaluate().rewards['correct'] == 1
        task.close()
    finally:
        bench.close()


def test_failed_later_step_cannot_return_an_earlier_reward(tmp_path):
    path = make_task(tmp_path, steps=True)
    # The first step succeeds. The second verifier produces no reward, which
    # Harbor records on StepResult rather than the top-level trial result.
    (path / 'steps/second/tests/test.sh').write_text('#!/bin/bash\nexit 1\n')
    bench = Benchmark('harbor', source=path, memory=Memory('256MiB', '256MiB'))
    try:
        task = bench.next()
        task.env.run('echo 1 > /answer', check=True)
        assert task.evaluate().rewards['correct'] == 1
        task.next_step()
        with pytest.raises(RuntimeError, match='Reward.*NotFoundError'):
            task.evaluate()
        task.close()
    finally:
        bench.close()


@pytest.mark.parametrize('steps', [False, True])
@pytest.mark.parametrize('dataset', [False, True])
def test_disabled_verification_needs_no_verifier_files(tmp_path, steps, dataset):
    import shutil
    path = make_task(tmp_path, steps=steps)
    for tests in path.rglob('tests'):
        shutil.rmtree(tests)
    bench = Benchmark('harbor', source=tmp_path if dataset else path, memory=Memory('256MiB', '256MiB'),
                      harbor={'verifier': {'disable': True}})
    try:
        task = bench.next()
        for index in range(2 if steps else 1):
            assert task.env.run('echo usable', check=True).stdout == 'usable\n'
            result = task.evaluate()
            assert result.skipped and result.rewards == {} and result.score is None
            if steps and not index:
                task.next_step()
        task.close()
    finally:
        bench.close()


def test_compose_uses_upstream_resource_overlay_and_task_precedence(tmp_path):
    from harbor.models.trial.config import ResourceMode
    from sandweave.benchmarks.harbor.compose import render
    from test_harbor import provider
    path = make_task(tmp_path)
    env = provider(path, override_cpus=2, override_memory_mb=384)
    service = render(env)['services']['main']
    assert float(service['cpus']) == 2
    assert int(service['mem_limit']) == 384 * 1024**2
    (path / 'environment/docker-compose.yaml').write_text('''services:
  main:
    cpus: 0.5
    mem_limit: 256m
''')
    service = render(env)['services']['main']
    assert float(service['cpus']) == .5
    assert int(service['mem_limit']) == 256 * 1024**2
    (path / 'environment/docker-compose.yaml').unlink()
    env = provider(path, cpu_enforcement_policy=ResourceMode.IGNORE,
                   memory_enforcement_policy=ResourceMode.IGNORE)
    service = render(env)['services']['main']
    assert not service.get('cpus') and not service.get('mem_limit')


@pytest.mark.parametrize('steps', [False, True])
def test_native_harbor_trial_and_oracle_use_the_same_provider(tmp_path, steps):
    from harbor.models.trial.config import TrialConfig
    from harbor.trial.trial import Trial
    path = make_task(tmp_path, steps=steps)
    roots = [path / 'steps/first', path / 'steps/second'] if steps else [path]
    for index, root in enumerate(roots):
        solution = root / 'solution'
        solution.mkdir()
        (solution / 'solve.sh').write_text(f'#!/bin/bash\nprintf {index + 1} > /answer\n')
    config = TrialConfig.model_validate({
        'task': {'path': path}, 'agent': {'name': 'oracle'},
        'environment': {'import_path': 'sandweave.benchmarks.harbor.provider:SandweaveEnvironment'},
        'trials_dir': tmp_path / 'trials'})
    async def run():
        trial = await Trial.create(config)
        result = await trial.run()
        assert result.exception_info is None, result.exception_info
        assert result.verifier_result.rewards['correct'] == 1
        assert all(step.exception_info is None for step in result.step_results or [])
        assert trial.agent_environment.sandbox is None
    asyncio.run(run())

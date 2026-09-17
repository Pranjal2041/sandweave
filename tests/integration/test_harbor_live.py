"""Unmodified Harbor Trial + verifier run against disposable Sandweave guests."""
import asyncio
import json
import os
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip('harbor')
from test_harbor import make_task
from sandweave import Benchmark, Memory, Sandbox
from test_weave_live import cluster
from sandweave.weave.transport import join_link

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_HARBOR_INTEGRATION'), reason='explicit disposable worker required')]


def test_positive_negative_rewards_hidden_tests_and_cleanup(tmp_path):
    make_task(tmp_path, 'a')
    make_task(tmp_path, 'b')
    bench = Benchmark('harbor', source=tmp_path, capacity=2, memory=Memory('256MiB', '256MiB'))
    try:
        task = bench.next()
        assert task.env.run('test ! -e /tests/test.sh').returncode == 0
        task.env.run('printf 1 > /answer', check=True)
        assert task.evaluate().rewards == {'correct': 1, 'cost': .25}
        assert task.evaluate().score is None
        task.close()
        task = bench.next()
        assert task.env.run('test ! -e /answer').returncode == 0
        assert task.evaluate().rewards == {'correct': 0, 'cost': .25}
        task.env.close()
    finally:
        bench.close()
    assert not bench._pool.live and not bench._attempts


def test_multi_step_keeps_guest_and_cleans_verifier_inputs(tmp_path):
    make_task(tmp_path, steps=True)
    bench = Benchmark('harbor', source=tmp_path, memory=Memory('256MiB', '256MiB'))
    try:
        task = bench.next()
        identity = task.env.id
        assert task.instruction == 'Write 1 into /answer'
        task.env.run('printf 1 > /answer', check=True)
        assert task.evaluate().rewards['correct'] == 1
        assert task.next_step() is task
        assert task.instruction == 'Write 2 into /answer'
        assert task.env.id == identity
        assert task.env.run('cat /answer').stdout == '1'
        assert task.env.run('test ! -e /tests/test.sh').returncode == 0
        task.env.run('printf 2 > /answer', check=True)
        assert task.evaluate().rewards['correct'] == 1
        with pytest.raises(StopIteration):
            task.next_step()
        task.close()
    finally:
        bench.close()


def exercise_concurrent(tmp_path, target=None):
    for index in range(12):
        make_task(tmp_path, f'task-{index:02}', image='ubuntu:22.04' if index % 2 else 'debian:bookworm-slim')
    bench = Benchmark('harbor', source=tmp_path, capacity=4, target=target,
                      memory=Memory('256MiB', '256MiB'))
    try:
        initial = [bench.next() for _ in range(4)]
        with pytest.raises(TimeoutError):
            bench.next(timeout=.05)
        with Sandbox(memory=Memory('256MiB', '256MiB')) as unrelated:
            assert unrelated.run('printf responsive').stdout == 'responsive'
        for task in initial:
            task.close()
        barrier = threading.Barrier(4)
        identities = set()
        lock = threading.Lock()
        def solve(_):
            task = None
            try:
                task = bench.next(timeout=120)
                with lock:
                    assert task.env.id not in identities
                    identities.add(task.env.id)
                assert task.env.run('test ! -e /answer').returncode == 0
                task.env.run('printf 1 > /answer', check=True)
                barrier.wait(timeout=120)
                assert task.evaluate().rewards['correct'] == 1
            except BaseException:
                barrier.abort()
                raise
            finally:
                if task is not None:
                    task.close()
        with ThreadPoolExecutor(4) as executor:
            list(executor.map(solve, range(8)))
        assert len(identities) == 8
        assert len(bench._pool.prepared) == 2
    finally:
        bench.close()
    assert not bench._attempts and not bench._pool.live


def test_concurrent_mixed_images_share_one_capacity(tmp_path):
    exercise_concurrent(tmp_path)


@pytest.mark.skipif(not os.environ.get('SANDWEAVE_WEAVE_INTEGRATION'), reason='explicit cluster required')
def test_http_concurrent_mixed_images_and_cleanup(tmp_path, cluster):
    url = join_link(cluster.info['connection']['address'], cluster.connection.token)
    exercise_concurrent(tmp_path, url)
    assert all(pool['state'] == 'closed' for pool in cluster.info['pools'])


def test_async_waiters_cancellation(tmp_path):
    for index in range(8):
        make_task(tmp_path, str(index))
    async def run():
        bench = Benchmark('harbor', source=tmp_path, capacity=1, memory=Memory('256MiB', '256MiB'))
        try:
            task = await bench.next.aio()
            waiters = [asyncio.create_task(bench.next.aio()) for _ in range(64)]
            await asyncio.sleep(.1)
            with pytest.raises(TimeoutError):
                await bench.next.aio(timeout=.05)
            assert await asyncio.wait_for(asyncio.to_thread(lambda: True), 5)
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
            await task.env.run.aio('printf 1 > /answer', check=True)
            assert (await task.evaluate.aio()).rewards['correct'] == 1
            await task.close.aio()
            task = await bench.next.aio()
            assert task.id == '1'
            await task.env.close.aio()
        finally:
            await bench.close.aio()
    asyncio.run(run())


def test_transfers_keep_modes_links_and_complete_output(tmp_path):
    task_path = make_task(tmp_path / 'data')
    inputs = task_path / 'environment'
    (inputs / 'empty').mkdir()
    (inputs / 'executable').write_text('#!/bin/bash\nprintf inherited')
    (inputs / 'executable').chmod(0o755)
    (inputs / 'link').symlink_to('executable')
    bench = Benchmark('harbor', source=task_path, memory=Memory('256MiB', '256MiB'))
    try:
        task = bench.next()
        assert task.env.run('test -d /empty && test -L /link && /link', check=True).stdout == 'inherited'
        provider = bench._pool.sessions[task.env.id].trial.agent_environment
        result = bench._suite.loop.call(provider.exec("head -c 2097152 /dev/zero | tr '\\0' x"))
        assert result.return_code == 0 and len(result.stdout) == 2097152
        task.env.run('mkdir -p /outputs/empty && cp -a /executable /link /outputs/', check=True)
        bench._suite.loop.call(provider.download_dir('/outputs', tmp_path / 'download'))
        assert (tmp_path / 'download/link').is_symlink()
        assert (tmp_path / 'download/executable').stat().st_mode & 0o111
        assert (tmp_path / 'download/empty').is_dir()
        task.close()
    finally:
        bench.close()


def test_separate_verifier_uses_another_clean_sandbox(tmp_path):
    path = make_task(tmp_path)
    config = path / 'task.toml'
    config.write_text(config.read_text() + '''
[verifier.environment]
docker_image = "ubuntu:22.04"
workdir = "/tests"
memory_mb = 256
''')
    (path / 'tests/test.sh').write_text('''#!/bin/bash
mkdir -p /logs/verifier
test ! -e /agent-private-file || exit 1
echo 1 > /logs/verifier/reward.txt
''')
    bench = Benchmark('harbor', source=path, memory=Memory('256MiB', '256MiB'))
    try:
        task = bench.next()
        task.env.run('touch /agent-private-file', check=True)
        result = task.evaluate()
        assert result.rewards == {'reward': 1.0}
        assert len(bench._pool.prepared) == 2
        task.close()
    finally:
        bench.close()


def test_phase_network_change_and_native_trial_options(tmp_path):
    path = make_task(tmp_path)
    config = path / 'task.toml'
    config.write_text(config.read_text().replace('[agent]\n', '[agent]\nnetwork_mode = "no-network"\n')
                      .replace('[verifier]\n', '[verifier]\nnetwork_mode = "public"\n'))
    (path / 'tests/test.sh').write_text('''#!/bin/bash
mkdir -p /logs/verifier
if timeout 10 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443'; then
    echo 1 > /logs/verifier/reward.txt
else
    echo 0 > /logs/verifier/reward.txt
fi
''')
    bench = Benchmark('harbor', source=path, memory=Memory('256MiB', '256MiB'),
        harbor={'extra_instructions': ['Additional client instruction.'],
                'agent': {'env': {'HARBOR_AGENT_SETTING': 'available'}}})
    try:
        task = bench.next()
        assert 'Additional client instruction.' in task.instruction
        assert task.env.run('printf "$HARBOR_AGENT_SETTING"').stdout == 'available'
        assert task.env.run("timeout 1 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443'").returncode != 0
        task.env.harbor.context.metadata = {'client': 'public pull loop'}
        assert task.evaluate().rewards == {'reward': 1.0}
        task.close()
    finally:
        bench.close()


def test_dockerfile_multistage_build_preserves_image_settings_and_deduplicates(tmp_path):
    path = make_task(tmp_path)
    config = path / 'task.toml'
    config.write_text(config.read_text().replace('docker_image = "ubuntu:22.04"\n', ''))
    (path / 'environment/Dockerfile').write_text('''FROM debian:bookworm-slim AS build
WORKDIR /build
COPY input.txt .
RUN tr a-z A-Z < input.txt > output.txt
FROM debian:bookworm-slim
RUN mkdir /app && chown 1000:1000 /app && echo 'client:x:1000:1000::/app:/bin/bash' >> /etc/passwd
COPY --from=build --chown=1000:1000 /build/output.txt /app/output.txt
ENV IMAGE_SETTING=retained
WORKDIR /app
USER 1000:1000
''')
    (path / 'environment/input.txt').write_text('built content')
    bench = Benchmark('harbor', source=path, capacity=2)
    try:
        def acquire(_):
            task = bench.task('task')
            task.__enter__()
            return task
        with ThreadPoolExecutor(2) as executor:
            tasks = list(executor.map(acquire, range(2)))
        for task in tasks:
            assert task.env.run('pwd; id -u; printf "$IMAGE_SETTING"; cat output.txt', check=True).stdout == '/app\n1000\nretainedBUILT CONTENT'
            task.env.run('printf 1 > /answer', user='root', check=True)
            assert task.evaluate().rewards['correct'] == 1
            task.close()
        assert len(bench._pool.builds) == 1
    finally:
        bench.close()


def exercise_compose(tmp_path, target=None):
    path = make_task(tmp_path, image='python:3.13-slim-bookworm')
    inputs = path / 'environment' / 'inputs'
    inputs.mkdir()
    (inputs / 'content').write_text('bind content')
    (path / 'environment/docker-compose.yaml').write_text('''services:
  main:
    depends_on:
      server:
        condition: service_healthy
    volumes:
      - shared:/shared
      - ./inputs:/inputs:ro
  server:
    image: python:3.13-slim-bookworm
    command: [python, -m, http.server, "8123", --directory, /shared]
    volumes:
      - shared:/shared
    healthcheck:
      test: [CMD, python, -c, "import socket; socket.create_connection(('127.0.0.1',8123),1).close()"]
      interval: 200ms
      retries: 20
volumes:
  shared: {}
''')
    config = path / 'task.toml'
    config.write_text(config.read_text().replace('version = "1.0"\n', '''version = "1.0"
artifacts = [{source = "/shared/value", service = "server", destination = "sidecar-value"}]
'''))
    bench = Benchmark('harbor', source=path, capacity=1, target=target)
    try:
        task = bench.next()
        assert task.env.run('cat /inputs/content', check=True).stdout == 'bind content'
        assert task.env.run('touch /inputs/forbidden').returncode != 0
        task.env.run('printf 1 > /shared/value', check=True)
        result = task.env.run("python -c 'import urllib.request; print(urllib.request.urlopen(\"http://server:8123/value\").read().decode())'", check=True)
        assert result.stdout.strip() == '1'
        task.env.run('cp /shared/value /answer', check=True)
        result = task.evaluate()
        assert result.rewards['correct'] == 1
        assert (Path(result.feedback) / 'artifacts/sidecar-value').read_text() == '1'
        task.close()
    finally:
        bench.close()


def test_compose_services_dns_volumes_healthcheck_and_sidecar_artifacts(tmp_path):
    exercise_compose(tmp_path)


def test_compose_initial_volume_contents_and_nonroot_ownership(tmp_path):
    path = make_task(tmp_path, image='python:3.13-slim-bookworm')
    (path / 'environment/Volume.Dockerfile').write_text('''FROM python:3.13-slim-bookworm
RUN mkdir /data && echo seeded > /data/value && ln /data/value /data/link && ln -s value /data/symlink && chown -Rh 1000:1000 /data && chmod 700 /data
USER 1000:1000
CMD ["python", "-m", "http.server", "8123", "--directory", "/data"]
''')
    (path / 'environment/docker-compose.yaml').write_text('''services:
  main:
    depends_on:
      database:
        condition: service_healthy
    volumes:
      - shared:/data
  database:
    build:
      context: .
      dockerfile: Volume.Dockerfile
    volumes:
      - shared:/data
    healthcheck:
      test: [CMD, python, -c, "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8123/value').read().strip()==b'seeded'"]
      interval: 200ms
      retries: 20
volumes:
  shared: {}
''')
    bench = Benchmark('harbor', source=path)
    try:
        task = bench.next()
        assert task.env.run("stat -c '%u:%g %a' /data", check=True).stdout.strip() == '1000:1000 700'
        assert task.env.run('cat /data/value', user='1000:1000', check=True).stdout == 'seeded\n'
        assert task.env.run('cat /data/value', user='2000:2000').returncode != 0
        assert task.env.run("python -c \"import os; assert os.stat('/data/value').st_ino==os.stat('/data/link').st_ino; assert os.lstat('/data/symlink').st_uid==1000\"", check=True).returncode == 0
        task.env.run('echo shared > /data/value', user='1000:1000', check=True)
        assert task.env.run("python -c \"import urllib.request; print(urllib.request.urlopen('http://database:8123/value').read().decode().strip())\"", check=True).stdout.strip() == 'shared'
        assert task.env.run('test ! -e /data/owners && test ! -e /data/../owners', check=True).returncode == 0
        task.close()
    finally:
        bench.close()


def test_compose_configs_secrets_tmpfs_hostname_and_readonly_root(tmp_path):
    path = make_task(tmp_path, image='python:3.13-slim-bookworm')
    (path / 'environment/token').write_text('private task input')
    (path / 'environment/docker-compose.yaml').write_text(json.dumps({
        'services': {
            'main': {'depends_on': {'service': {'condition': 'service_healthy'}}},
            'service': {
                'image': 'python:3.13-slim-bookworm', 'user': '1000:1000',
                'hostname': 'task-sidecar', 'extra_hosts': {'fixture.invalid': '192.0.2.42'},
                'read_only': True, 'tmpfs': ['/scratch:size=8m,mode=1777'],
                'configs': [{'source': 'settings', 'target': '/etc/task-settings'}],
                'secrets': [{'source': 'token', 'uid': '1000', 'gid': '1000', 'mode': 0o440}],
                'command': ['python', '-m', 'http.server', '8123', '--directory', '/scratch'],
                'healthcheck': {'test': ['CMD', 'python', '-c',
                    "import pathlib,socket,os; assert pathlib.Path('/etc/task-settings').read_text()=='fixture configuration'; "
                    "assert pathlib.Path('/run/secrets/token').read_text()=='private task input'; "
                    "assert os.stat('/run/secrets/token').st_uid==1000; "
                    "assert socket.gethostname()=='task-sidecar'; "
                    "assert socket.gethostbyname('fixture.invalid')=='192.0.2.42'; "
                    "pathlib.Path('/scratch/ready').write_text('ready'); socket.create_connection(('127.0.0.1',8123),1).close()"],
                    'interval': '200ms', 'retries': 20}}},
        'configs': {'settings': {'content': 'fixture configuration'}},
        'secrets': {'token': {'file': './token'}}}))
    bench = Benchmark('harbor', source=path)
    try:
        task = bench.next()
        assert task.env.run("python -c \"import urllib.request; print(urllib.request.urlopen('http://service:8123/ready').read().decode())\"", check=True).stdout.strip() == 'ready'
        provider = bench._pool.sessions[task.env.id].trial.agent_environment
        assert bench._suite.loop.call(provider.service_exec('touch /rootfs-must-be-readonly', service='service', user='root')).return_code != 0
        assert bench._suite.loop.call(provider.service_exec('touch /scratch/writable', service='service', user='1000:1000')).return_code == 0
        task.close()
    finally:
        bench.close()


def test_service_entrypoint_exit_stops_commands_and_keeps_artifacts(tmp_path):
    path = make_task(tmp_path)
    (path / 'environment/docker-compose.yaml').write_text('''services:
  main:
    command: [sh, -c, "echo ready > /artifact; while test ! -e /finish; do sleep 0.05; done"]
''')
    bench = Benchmark('harbor', source=path)
    try:
        task = bench.next()
        background = task.env.exec('sleep 1000')
        task.env.run('touch /finish', check=True)
        background.wait(timeout=10)
        assert background.result().returncode != 0
        with pytest.raises(Exception, match='service is stopped'):
            task.env.run('echo should-not-run')
        provider = bench._pool.sessions[task.env.id].trial.agent_environment
        bench._suite.loop.call(provider.download_file('/artifact', tmp_path / 'artifact'))
        assert (tmp_path / 'artifact').read_text() == 'ready\n'
        bench._suite.loop.call(provider.download_dir('/logs', tmp_path / 'logs'))
        task.close()
    finally:
        bench.close()


def test_compose_build_contexts_secrets_inline_recipe_and_offline_build(tmp_path):
    path = make_task(tmp_path)
    config = path / 'task.toml'
    config.write_text(config.read_text().replace('docker_image = "ubuntu:22.04"\n', ''))
    (path / 'environment/inputs').mkdir()
    (path / 'environment/inputs/value').write_text('extra build context')
    (path / 'environment/build-token').write_text('build secret')
    recipe = '''FROM ubuntu:22.04
ARG SELECTED
RUN --mount=type=bind,from=fixtures,target=/input --mount=type=secret,id=token,required=true \\
    test "$(cat /run/secrets/token)" = "build secret" && cp /input/value /built && echo "$$SELECTED" > /argument
RUN test ! -e /run/secrets/token && ! timeout 1 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443'
'''
    (path / 'environment/docker-compose.yaml').write_text(json.dumps({
        'services': {'main': {'build': {'context': '.', 'dockerfile_inline': recipe,
            'network': 'none', 'args': {'SELECTED': 'preserved'},
            'additional_contexts': {'fixtures': './inputs'}, 'secrets': [{'source': 'token'}]}}},
        'secrets': {'token': {'file': './build-token'}}}))
    bench = Benchmark('harbor', source=path, memory=Memory('256MiB', '256MiB'))
    try:
        task = bench.next()
        assert task.env.run('cat /built', check=True).stdout == 'extra build context'
        assert task.env.run('cat /argument', check=True).stdout == 'preserved\n'
        assert task.env.run('test ! -e /run/secrets/token', check=True).returncode == 0
        task.env.run('echo 1 > /answer', check=True)
        assert task.evaluate().rewards['correct'] == 1
        task.close()
    finally:
        bench.close()


def test_mcp_and_skills_inputs_reach_the_client_with_native_context(tmp_path):
    path = make_task(tmp_path, image='python:3.13-slim-bookworm')
    config = path / 'task.toml'
    config.write_text(config.read_text().replace('[environment]\n', '''[environment]
workdir = "/task-inputs"
skills_dir = "/task-inputs/skills"
mcp_servers = [{name = "fixture", transport = "stdio", command = "python", args = ["/task-inputs/mcp.py"]}]
'''))
    skills = path / 'environment/skills/tool'
    skills.mkdir(parents=True)
    (skills / 'SKILL.md').write_text('Use the fixture tool to obtain the task input.')
    (path / 'environment/mcp.py').write_text('''import json,sys
for line in sys.stdin:
    request = json.loads(line)
    print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'tools':[]}}),flush=True)
''')
    bench = Benchmark('harbor', source=path, memory=Memory('256MiB', '256MiB'))
    try:
        task = bench.next()
        inputs = task.env.harbor
        assert inputs.skills_dir == '/task-inputs/skills'
        assert 'fixture tool' in task.env.files.read_text(inputs.skills_dir + '/tool/SKILL.md')
        server, = inputs.mcp_servers
        assert server.name == 'fixture' and server.transport == 'stdio'
        process = task.env.exec(argv=[server.command, *server.args])
        process.stdin.write('{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n')
        process.stdin.close()
        process.wait(timeout=10)
        assert json.loads(process.result().stdout)['result'] == {'tools': []}
        assert inputs.context is not None and inputs.logs_dir == '/logs/agent'
        task.env.run('echo 1 > /answer', check=True)
        assert task.evaluate().rewards['correct'] == 1
        task.close()
    finally:
        bench.close()


def test_allowlist_dns_tcp_and_verifier_network_transition(tmp_path):
    path = make_task(tmp_path, image='python:3.13-slim-bookworm')
    config = path / 'task.toml'
    config.write_text(config.read_text().replace('[agent]\n', '''[agent]
network_mode = "allowlist"
allowed_hosts = ["pypi.org", "*.pythonhosted.org"]
''').replace('[verifier]\n', '[verifier]\nnetwork_mode = "public"\n'))
    (path / 'tests/test.sh').write_text('''#!/bin/bash
mkdir -p /logs/verifier
python -c "import socket; socket.create_connection(('1.1.1.1',443),5).close()" || exit 1
echo 1 > /logs/verifier/reward.txt
''')
    bench = Benchmark('harbor', source=path, memory=Memory('256MiB', '256MiB'))
    try:
        task = bench.next()
        assert task.env.run("python -c \"import urllib.request; print(urllib.request.urlopen('https://pypi.org/simple/',timeout=10).status)\"", check=True).stdout.strip() == '200'
        assert task.env.run("python -c \"import socket; socket.create_connection(('1.1.1.1',443),1)\"").returncode != 0
        assert task.env.run("python -c \"import socket; socket.create_connection(('files.pythonhosted.org',443),5).close()\"", check=True).returncode == 0
        assert task.evaluate().rewards == {'reward': 1.0}
        task.close()
    finally:
        bench.close()


def test_failed_compose_healthcheck_releases_group(tmp_path):
    from sandweave.sandbox.targets import connect
    connection = connect(None)
    before = {record['id'] for record in connection.call('inventory')['live']}
    path = make_task(tmp_path)
    (path / 'environment/docker-compose.yaml').write_text('''services:
  main:
    depends_on:
      sidecar:
        condition: service_healthy
  sidecar:
    image: ubuntu:22.04
    command: [sleep, infinity]
    healthcheck:
      test: [CMD, "false"]
      interval: 50ms
      retries: 2
''')
    bench = Benchmark('harbor', source=path)
    try:
        with pytest.raises(RuntimeError, match='healthcheck failed'):
            bench.next()
    finally:
        bench.close()
        try:
            assert {record['id'] for record in connection.call('inventory')['live']} == before
        finally:
            connection.close()


@pytest.mark.skipif(not os.environ.get('SANDWEAVE_WEAVE_INTEGRATION'), reason='explicit cluster required')
def test_http_compose_group_reserves_and_releases_all_services(tmp_path, cluster):
    url = join_link(cluster.info['connection']['address'], cluster.connection.token)
    exercise_compose(tmp_path, url)
    from test_weave_live import wait_for
    wait_for(lambda: all(row['released'] for row in cluster.info['sandboxes']))
    for worker in cluster.test_workers:
        assert not worker.call('inventory')['live']


@pytest.mark.skipif(not os.environ.get('SANDWEAVE_WEAVE_INTEGRATION'), reason='explicit cluster required')
@pytest.mark.parametrize('cluster', ['8GiB'], indirect=True)
def test_http_build_transfers_to_another_worker_before_trial(tmp_path, cluster):
    from sandweave.templates.build import build
    from test_weave_live import wait_for
    path = make_task(tmp_path)
    config = path / 'task.toml'
    config.write_text(config.read_text().replace('docker_image = "ubuntu:22.04"\n', ''))
    (path / 'environment/Dockerfile').write_text('FROM debian:bookworm-slim\nRUN echo original > /built\n')
    url = join_link(cluster.info['connection']['address'], cluster.connection.token)
    previous = {row['id'] for row in cluster.info['sandboxes']}
    saved = build(path / 'environment', target=url, template={'name': 'harbor', 'command_shell': '/bin/bash'})
    builder = next(row for row in cluster.info['sandboxes'] if row['id'] not in previous)
    cluster.drain(builder['worker'])
    bench = Benchmark('harbor', source=path, target=url, memory=Memory('256MiB', '256MiB'))
    try:
        task = bench.next()
        allocation = next(row for row in cluster.info['sandboxes'] if row['id'] == task.env.id)
        assert allocation['worker'] != builder['worker']
        assert task.env.run('cat /built', check=True).stdout == 'original\n'
        task.env.run('echo 1 > /answer', check=True)
        assert task.evaluate().rewards['correct'] == 1
        task.close()
    finally:
        bench.close()
        saved._connection.close()
        cluster.resume(builder['worker'])
    wait_for(lambda: all(row['released'] for row in cluster.info['sandboxes']))


@pytest.mark.parametrize('asynchronous', [False, True])
def test_mapping_runs_all_steps(asynchronous, tmp_path):
    make_task(tmp_path, steps=True)
    bench = Benchmark('harbor', source=tmp_path, memory=Memory('256MiB', '256MiB'))
    def agent(env, instruction):
        env.run('printf ' + instruction.split()[1] + ' > /answer', check=True)
    async def run():
        return [result async for result in bench.map.aio(agent)]
    try:
        results = asyncio.run(run()) if asynchronous else list(bench.map(agent))
        assert len(results) == 1 and results[0].rewards['correct'] == 1
    finally:
        bench.close()


@pytest.mark.skipif(not os.environ.get('SANDWEAVE_HARBOR_TASK'), reason='explicit unchanged upstream task required')
def test_original_task_solution_and_verifier():
    source = Path(os.environ.get('SANDWEAVE_HARBOR_TASK', '.'))
    bench = Benchmark('harbor', source=source, capacity=1)
    try:
        task = bench.next()
        assert task.env.run('test ! -e /tests/test.sh').returncode == 0
        provider = bench._pool.sessions[task.env.id].trial.agent_environment
        bench._suite.loop.call(provider.upload_dir(source / 'solution', '/solution'))
        task.env.run('bash /solution/solve.sh', timeout=300, check=True)
        result = task.evaluate()
        assert result.rewards == {'reward': 1.0}, result
        print('Original solution:', result)
        task.close()
        retry = bench.task(task.id)
        with retry:
            result = retry.evaluate()
            assert result.rewards == {'reward': 0.0}, result
            print('Unsolved fresh task:', result)
    finally:
        bench.close()

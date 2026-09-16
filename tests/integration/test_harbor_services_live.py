"""Service lifecycle semantics through the public benchmark pull API."""
import os
import json
import time

import pytest

pytest.importorskip('harbor')
from sandweave import Benchmark, Memory
from test_harbor import make_task

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_HARBOR_INTEGRATION'), reason='explicit disposable worker required')]


def test_restart_limits_groups_and_graceful_stop(tmp_path):
    path = make_task(tmp_path, image='python:3.13-slim-bookworm')
    command = '''import pathlib,signal,time,sys
count=pathlib.Path('/scratch/count')
attempt=int(count.read_text())+1 if count.exists() else 1
count.write_text(str(attempt))
if attempt==1: sys.exit(1)
def finish(*args):
    pathlib.Path('/scratch/finalized').write_text('complete')
    sys.exit(0)
signal.signal(signal.SIGUSR1,finish)
while True: time.sleep(.05)
'''
    (path / 'environment/docker-compose.yaml').write_text(json.dumps({'services': {
        'main': {'depends_on': {'worker': {'condition': 'service_healthy'}}},
        'worker': {'image': 'python:3.13-slim-bookworm', 'user': '1000:1000',
            'command': ['python', '-c', command], 'restart': 'on-failure:3',
            'stop_signal': 'SIGUSR1', 'stop_grace_period': '2s', 'group_add': ['7777'],
            'ulimits': {'nofile': {'soft': 512, 'hard': 512}}, 'tmpfs': ['/scratch:mode=1777'],
            'healthcheck': {'test': ['CMD', 'python', '-c',
                "import pathlib,resource,os; assert pathlib.Path('/scratch/count').read_text()=='2'; "
                "assert resource.getrlimit(resource.RLIMIT_NOFILE)==(512,512); assert 7777 in os.getgroups()"],
                'interval': '100ms', 'retries': 30}}}}))
    bench = Benchmark('harbor', source=path, memory=Memory('256MiB', '256MiB'))
    try:
        task = bench.next()
        provider = bench._pool.sessions[task.env.id].trial.agent_environment
        bench._suite.loop.call(provider.stop_service('worker'))
        destination = tmp_path / 'finalized'
        bench._suite.loop.call(provider.service_download_file('/scratch/finalized', destination, service='worker'))
        assert destination.read_text() == 'complete'
        time.sleep(.3)
        bench._suite.loop.call(provider.service_download_file('/scratch/count', tmp_path / 'count', service='worker'))
        assert (tmp_path / 'count').read_text() == '2'
        assert bench._suite.loop.call(provider.service_is_dir('/scratch', service='worker'))
        bench._suite.loop.call(provider.service_download_dir_with_exclusions(
            source_dir='/scratch', target_dir=tmp_path / 'filtered', exclude=['count'], service='worker'))
        assert (tmp_path / 'filtered/finalized').read_text() == 'complete'
        assert not (tmp_path / 'filtered/count').exists()
        with pytest.raises(Exception, match='service is stopped'):
            bench._suite.loop.call(provider.service_exec('true', service='worker'))
        task.close()
    finally:
        bench.close()


def test_remote_git_build_context_uses_original_upstream_dockerfile(tmp_path):
    path = make_task(tmp_path)
    config = path / 'task.toml'
    config.write_text(config.read_text().replace('docker_image = "ubuntu:22.04"\n', ''))
    context = 'https://github.com/harbor-framework/harbor.git#cfc54c995e90cd438deb862189a58053b9d89fd3:examples/tasks/sidecar-artifacts/environment'
    (path / 'environment/docker-compose.yaml').write_text(json.dumps({
        'services': {'main': {'build': {'context': context}}}}))
    bench = Benchmark('harbor', source=path, memory=Memory('256MiB', '256MiB'))
    try:
        task = bench.next()
        assert task.env.run('curl --version', check=True).stdout.startswith('curl ')
        assert task.env.run('cat /etc/os-release', check=True).stdout.find('24.04') >= 0
        task.env.run('echo 1 > /answer', check=True)
        assert task.evaluate().rewards['correct'] == 1
        task.close()
    finally:
        bench.close()


def test_readonly_main_retains_writable_harbor_log_mounts(tmp_path):
    path = make_task(tmp_path, image='python:3.13-slim-bookworm')
    (path / 'environment/docker-compose.yaml').write_text('''services:
  main:
    read_only: true
    working_dir: /created-before-readonly
''')
    bench = Benchmark('harbor', source=path)
    try:
        task = bench.next()
        assert task.env.run('pwd', check=True).stdout.strip() == '/created-before-readonly'
        assert task.env.run('touch /should-fail').returncode != 0
        task.env.run('echo preserved > /logs/artifacts/value', check=True)
        assert task.env.files.read_text('/logs/artifacts/value') == 'preserved\n'
        task.close()
        assert any(file.read_text() == 'preserved\n' for file in bench._pool.output.rglob('value'))
    finally:
        bench.close()


def test_service_exit_reaps_detached_descendants_before_artifact_export(tmp_path):
    path = make_task(tmp_path, image='python:3.13-slim-bookworm')
    command = '''import os,pathlib,time
child=os.fork()
if child==0:
    os.setsid()
    if os.fork(): os._exit(0)
    pathlib.Path('/tmp/detached-pid').write_text(str(os.getpid()))
    while True:
        pathlib.Path('/tmp/heartbeat').write_text(str(time.monotonic_ns()))
        time.sleep(.01)
os.waitpid(child,0)
while True: time.sleep(.05)
'''
    (path / 'environment/docker-compose.yaml').write_text(json.dumps({'services': {
        'main': {'depends_on': {'worker': {'condition': 'service_healthy'}}},
        'worker': {'image': 'python:3.13-slim-bookworm', 'command': ['python', '-c', command],
            'healthcheck': {'test': ['CMD', 'test', '-f', '/tmp/heartbeat'], 'interval': '100ms'}}}}))
    bench = Benchmark('harbor', source=path, memory=Memory('256MiB', '256MiB'))
    try:
        task = bench.next()
        provider = bench._pool.sessions[task.env.id].trial.agent_environment
        bench._suite.loop.call(provider.stop_service('worker'))
        time.sleep(.3)
        bench._suite.loop.call(provider.service_download_file('/tmp/heartbeat', tmp_path / 'first', service='worker'))
        time.sleep(.2)
        bench._suite.loop.call(provider.service_download_file('/tmp/heartbeat', tmp_path / 'second', service='worker'))
        assert (tmp_path / 'first').read_bytes() == (tmp_path / 'second').read_bytes()
        task.close()
    finally:
        bench.close()


def test_extra_compose_overlay_preserves_prebuilt_environment_inputs(tmp_path):
    path = make_task(tmp_path, image='python:3.13-slim-bookworm')
    (path / 'environment/input.txt').write_text('original task input\n')
    overlay = tmp_path / 'overlay.yaml'
    overlay.write_text('services:\n  main:\n    working_dir: /task-inputs\n')
    bench = Benchmark('harbor', source=path, harbor={
        'environment': {'kwargs': {'extra_docker_compose': [str(overlay)]}}})
    try:
        task = bench.next()
        assert task.env.run('cat input.txt', check=True).stdout == 'original task input\n'
        task.env.run('echo 1 > /answer', check=True)
        assert task.evaluate().rewards['correct'] == 1
        task.close()
    finally:
        bench.close()

"""Live Weave acceptance on two isolated workers in an existing allocation."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import pytest

from sandweave import Cluster, Sandbox, Pool, Job
from sandweave.sandbox.connection import Connection

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_WEAVE_INTEGRATION'), reason='explicit isolated workers required')]


def package_path():
    import sandweave
    return str(Path(sandweave.__file__).resolve().parent.parent)


def wait_for(function, *, timeout=180):
    deadline = time.monotonic() + timeout
    while not function():
        if time.monotonic() > deadline:
            raise TimeoutError('live acceptance condition timed out')
        time.sleep(.1)


@pytest.fixture(scope='module')
def cluster(tmp_path_factory):
    root = Path(os.environ['SANDWEAVE_WEAVE_INTEGRATION']).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # A module-scoped fixture can run several times in one invocation. Give
    # each controller fresh records instead of inheriting stopped workers from
    # the previous module or an earlier acceptance run.
    root = Path(tempfile.mkdtemp(prefix='workers-', dir=root))
    controller_home = root / 'client'
    previous = os.environ.get('SANDWEAVE_HOME')
    os.environ['SANDWEAVE_HOME'] = str(controller_home)
    cpus = sorted(os.sched_getaffinity(0))
    assert len(cpus) >= 4
    processes, connections, logs = [], [], []
    bridges = []
    # Parallel pytest runs garbage-collect each other's older temporary roots.
    # Keep a daemon's state with its explicitly selected integration artifacts.
    controller_directory = os.environ.get('SANDWEAVE_WEAVE_CONTROLLER_DIRECTORY') or root / 'controller'
    cluster = Cluster.start('weave-live', directory=controller_directory, local_worker=False)
    try:
        for index in range(2):
            worker_home = root / ('worker-' + str(index))
            worker_home.mkdir(exist_ok=True)
            marker = worker_home / 'worker.json'
            log = (worker_home / 'worker.log').open('ab')
            logs.append(log)
            environment = {**os.environ, 'SANDWEAVE_HOME': str(worker_home),
                           'SANDWEAVE_ASSETS': os.environ.get('SANDWEAVE_ASSETS', str(Path.cwd())),
                           'SANDWEAVE_MEMORY_BUDGET': '4GiB',
                           'PYTHONPATH': package_path()}
            child = subprocess.Popen(['taskset', '-c', ','.join(map(str, cpus[index*2:index*2+2])),
                sys.executable, '-m', 'sandweave.sandbox.worker', '--metadata', str(marker)],
                env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True)
            processes.append(child)
            def ready():
                if child.poll() is not None:
                    raise RuntimeError((worker_home / 'worker.log').read_text()[-5000:])
                return marker.exists() and json.loads(marker.read_text()).get('pid') == child.pid
            wait_for(ready, timeout=float(os.environ.get('SANDWEAVE_WEAVE_STARTUP_TIMEOUT', '180')))
            info = json.loads(marker.read_text())
            connection = Connection('127.0.0.1', info['port'], info['token'])
            connections.append(connection)
            endpoint = {'hostname': socket.gethostname(), 'port': info['port'], 'token': info['token']}
            if os.environ.get('SANDWEAVE_WEAVE_RELAY'):
                from sandweave.weave.worker import Bridge
                endpoint['relay'] = uuid.uuid4().hex
                bridges.append(Bridge({'url': cluster.info['connection']['address'],
                    'token': cluster.connection.token}, endpoint['relay']).start())
            cluster.add_worker({'endpoint': endpoint},
                               slots=2, memory='4GiB', name='acceptance-' + str(index))
        cluster.test_workers = connections
        yield cluster
    finally:
        # Each worker above belongs solely to this acceptance run. Terminate
        # only its records, then use the worker's guarded idle-shutdown operation.
        try:
            for job in cluster.info['jobs']:
                cluster.connection.call('job_cancel', identity=job['id'])
            for pool in cluster.info['pools']:
                cluster.connection.call('pool_close', identity=pool['id'])
            for env in cluster.info['sandboxes']:
                cluster.connection.call('allocation_cancel', identity=env['id'])
            wait_for(lambda: all(a['released'] for a in cluster.info['sandboxes']))
            cluster.stop()
        finally:
            cluster.close()
            for bridge in bridges:
                bridge.close()
            for connection in connections:
                for record in connection.call('list'):
                    if record['state'] not in ('terminated', 'stopped'):
                        connection.call('terminate', identity=record['id'])
                connection.call('_shutdown_if_idle')
                connection.close()
            for index, child in enumerate(processes):
                # A preparation failure can occur before the worker publishes
                # an endpoint. It has not received any sandbox requests, but it
                # still belongs to this fixture and must not be left behind.
                if index >= len(connections) and child.poll() is None:
                    child.terminate()
                child.wait(timeout=30)
            for log in logs:
                log.close()
            if previous is None:
                os.environ.pop('SANDWEAVE_HOME', None)
            else:
                os.environ['SANDWEAVE_HOME'] = previous


def test_cluster_routes_commands_and_files_directly(cluster):
    def run(index):
        with Sandbox(target='weave-live') as env:
            env.files.write_text('/workspace/value', str(index))
            result = env.run("python -c 'print(2 + 2)' && sleep .2")
            assert result.stdout == '4\n'
            assert env.files.read_text('/workspace/value') == str(index)
            assert env.run('test ! -e /dev/kvm').returncode == 0
            return env.id
    with ThreadPoolExecutor(4) as executor:
        ids = list(executor.map(run, range(4)))
    records = [a for a in cluster.info['sandboxes'] if a['id'] in ids]
    assert len({a['worker'] for a in records}) == 2


def test_pool_keeps_pristine_state_and_async_contract(cluster):
    import asyncio
    with Pool(target='weave-live', size=2, warm=1) as pool:
        assert pool.info['ready'] >= 1
        def evaluate(env, value):
            assert env.run('test ! -e /workspace/task').returncode == 0
            env.files.write_text('/workspace/task', str(value))
            return env.run('cat /workspace/task').stdout
        assert list(pool.map(evaluate, [1, 2, 3])) == ['1', '2', '3']
        async def exercise():
            async with pool.acquire() as env:
                assert (await env.run.aio('printf asynchronous')).stdout == 'asynchronous'
        asyncio.run(exercise())
    assert pool.info['state'] == 'closed'


def test_docker_image_transfer_pool_and_job(cluster):
    with Sandbox(target='weave-live', image='docker://busybox:1.37.0', startup_timeout=900) as source:
        source.files.write_text('/saved', 'image state')
        saved = source.snapshot(state='filesystem')
        worker = next(a['worker'] for a in cluster.info['sandboxes'] if a['id'] == source.id)
        digest = source.info['image']['digest']
        cluster.drain(worker)
        try:
            with Sandbox(target='weave-live', snapshot=saved, startup_timeout=900) as restored:
                assert restored.files.read_text('/saved') == 'image state'
                assert restored.info['image']['digest'] == digest
                destination = next(a['worker'] for a in cluster.info['sandboxes'] if a['id'] == restored.id)
                assert destination != worker
                assert restored.run('echo transferred').stdout == 'transferred\n'
        finally:
            cluster.resume(worker)
    with Pool(target='weave-live', image='docker://busybox:1.37.0',
              size=2, warm=1, startup_timeout=900) as pool:
        with pool.acquire() as env:
            assert env.run('echo pooled').stdout == 'pooled\n'
            assert env.info['image']['digest'] == digest
        job = pool.submit('cat /workspace/message; echo error >&2; exit 7', files={'message': b'uploaded\n'})
        try:
            result = job.result(timeout=120)
            assert (result.stdout, result.stderr, result.returncode) == ('uploaded\n', 'error\n', 7)
        finally:
            job.close()


def test_durable_job_survives_controller_crash_and_preserves_stderr(cluster, tmp_path):
    import signal
    from sandweave.sandbox.ownership import process_alive
    with Pool(target='weave-live', size=2, warm=1, detached=True) as pool:
        job = pool.submit('echo started; sleep 4; echo problem >&2; exit 7', detached=True)
        wait_for(lambda: any(t['state'] == 'running' for t in job.info['tasks']))
        marker = Path(cluster.config['directory']) / 'controller.json'
        information = json.loads(marker.read_text())
        command = Path('/proc/' + str(information['pid']) + '/cmdline').read_bytes().split(b'\0')
        assert b'sandweave.weave.server' in command
        assert cluster.config['directory'].encode() in command
        os.kill(information['pid'], signal.SIGKILL)
        wait_for(lambda: process_alive(information['process']) is False)
        replacement = Cluster.start('weave-live', directory=cluster.config['directory'], local_worker=False)
        replacement.close()
        result = Job.connect(job.id, target='weave-live').result(timeout=60)
        assert (result.stdout, result.stderr, result.returncode) == ('started\n', 'problem\n', 7)
        assert job.info['tasks'][0]['attempt'] == 0


def test_durable_batch_upload_retry_and_execution_deadline(cluster):
    from sandweave.sandbox.errors import CommandTimeout
    with Pool(target='weave-live', size=2, warm=1) as pool:
        program = b'import os\nprint(os.environ["SANDWEAVE_ITEM"])\n'
        job = pool.submit('python /workspace/evaluate.py', files={'evaluate.py': program}, items=[3, 1, 2])
        assert [r.stdout for r in job.result(timeout=90)] == ['3\n', '1\n', '2\n']
        retry = pool.submit('echo "$SANDWEAVE_ATTEMPT"; test "$SANDWEAVE_ATTEMPT" -gt 0 || exit 75',
                            retries=1, retry_codes=[75])
        assert retry.result(timeout=60).stdout == '1\n'
        assert retry.info['tasks'][0]['attempt'] == 1
        deadline = pool.submit('sleep 20', timeout=.5)
        with pytest.raises(CommandTimeout):
            deadline.result(timeout=60)


def test_cli_uses_the_same_cluster_target(cluster):
    environment = {**os.environ, 'PYTHONPATH': package_path()}
    result = subprocess.run([sys.executable, '-m', 'sandweave.cli', 'run', '--target', 'weave-live',
        '--no-stdin', '--', "echo output; echo problem >&2; exit 7"], env=environment,
        capture_output=True, text=True, timeout=90)
    assert (result.stdout, result.stderr, result.returncode) == ('output\n', 'problem\n', 7)


def test_filesystem_snapshot_full_transfer_and_restore(cluster):
    from sandweave.sandbox.errors import CacheMiss
    from sandweave.sandbox.targets import Endpoint
    with Sandbox(target='weave-live') as env:
        env.files.write_text('/workspace/saved', 'cross-worker state')
        saved = env.cache('transfer-check')
        assert saved.verify()['status'] == 'passed'
    source, destination = None, None
    for connection in cluster.test_workers:
        try:
            connection.call('snapshot_spec', reference=saved.id)
            source = connection
        except CacheMiss:
            destination = connection
    assert source is not None and destination is not None
    manifest = source.call('artifact_manifest', reference=saved.id)
    # Explicitly exercise byte transfer, bypassing shared-storage discovery.
    missing = destination.call('artifact_begin', manifest=manifest)
    transferred = 0
    for path in missing:
        size = manifest['files'][path]['size']
        for offset in range(0, size, 1024**2):
            data = source.call('artifact_read', reference=saved.id, path=path,
                               offset=offset, size=min(1024**2, size-offset))
            destination.call('artifact_write', reference=saved.id, path=path, offset=offset, data=data)
            transferred += len(data)
    assert transferred > 0
    destination.call('artifact_finish', reference=saved.id)
    imported = destination.call('snapshot_info', reference=saved.id)
    assert imported['location'] != saved.location
    assert imported['verification']['status'] == 'passed'
    # Repeated negotiation must use the same immutable manifest.
    assert destination.call('artifact_begin', manifest=source.call('artifact_manifest', reference=saved.id)) == []
    with Sandbox(cache=saved.id, target=Endpoint(destination.port, destination.token)) as restored:
        assert restored.files.read_text('/workspace/saved') == 'cross-worker state'


def test_job_creates_and_closes_its_own_pool(cluster):
    job = Job.submit('python /workspace/program.py', target='weave-live', detached=True,
                     files={'program.py': b'print("submitted")\n'})
    assert job.result(timeout=90).stdout == 'submitted\n'
    pool_id = job.info['pool']
    wait_for(lambda: cluster.connection.call('pool_status', identity=pool_id)['state'] == 'closed')

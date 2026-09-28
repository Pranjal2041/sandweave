"""Slow real cold restores obey their startup budget without blocking peers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import time

import pytest

from sandweave import Memory, Sandbox, SandboxError
from sandweave.sandbox import workspace
from sandweave.sandbox.targets import local_connection
from sandweave.sandbox.wire import decode

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit disposable worker required')]


@pytest.fixture(scope='module')
def delayed_worker():
    for parent in (workspace.home(), workspace.local_parent()):
        parent.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(parent)
        if usage.free - 8 * 1024**3 < usage.total * .15:
            pytest.skip('restore qualification would leave less than 15% free')
    # First-use preparation can select a different installed-asset workspace.
    # Obtain the control transport only after that selection has completed.
    with Sandbox():
        pass
    connection = local_connection()
    root = Path(connection.call('ping')['workspace'])
    wrapper = root / 'scripts/gvisor-host.sh'
    real = wrapper.with_name('restore-test-host.sh')
    policy = root / 'restore-test-delay.json'
    calls = root / 'restore-test-calls.jsonl'
    original, mode = wrapper.read_bytes(), wrapper.stat().st_mode
    real.write_bytes(original)
    real.chmod(mode)
    # Delay only the test worker's control transport. The packaged Python
    # restore implementation and the engine binary remain unchanged.
    wrapper.write_text('''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
root = Path(__file__).resolve().parent.parent
args = sys.argv[1:]
phase, identity = None, None
if 'read' in args and args[-1] == '/run/engine-fs-waiting':
    phase, identity = 'staging', args[args.index('read') + 1]
elif '--restore-mount' in args:
    phase, identity = 'unpacking', args[-1]
policy = root / 'restore-test-delay.json'
if phase and policy.exists():
    delay = json.loads(policy.read_text()).get(phase, 0)
    with (root / 'restore-test-calls.jsonl').open('a') as log:
        log.write(json.dumps({'phase': phase, 'identity': identity, 'delay': delay}) + '\\n')
    time.sleep(delay)
real = Path(__file__).with_name('restore-test-host.sh')
os.execv(str(real), [str(real), *args])
''')
    wrapper.chmod(mode)
    try:
        recipe = {'extends': 'coding', 'runtime_options': {'docker_data': True}}
        with Sandbox(template=recipe, memory=Memory('256MiB', '512MiB')) as source:
            source.run('echo root > /workspace/record; '
                       'echo docker > /var/lib/docker/record; '
                       'echo containerd > /var/lib/containerd/record; '
                       'dd if=/dev/zero of=/var/lib/docker/data bs=1M count=32', check=True)
            saved = source.snapshot(state='filesystem')
        yield root, policy, calls, saved
    finally:
        wrapper.write_bytes(original)
        wrapper.chmod(mode)
        real.unlink()
        policy.unlink(missing_ok=True)
        connection.call('_shutdown_if_idle')
        connection.close()


@pytest.mark.parametrize('phase', ['staging', 'unpacking'])
def test_restore_timeout_cleans_partial_guest_and_keeps_snapshot(delayed_worker, phase):
    root, policy, calls, saved = delayed_worker
    calls.write_text('')
    policy.write_text(json.dumps({phase: 20}))
    with pytest.raises((SandboxError, TimeoutError)):
        Sandbox(snapshot=saved, startup_timeout=8)
    attempts = [json.loads(line) for line in calls.read_text().splitlines()]
    matching = [call for call in attempts if call['phase'] == phase]
    assert matching, 'the timeout must exercise the intended restore phase'
    identity = matching[0]['identity']
    record = decode((root / 'sandboxes' / (identity + '.bin')).read_bytes())
    assert record['state'] in ('failed', 'terminated') and record['cleanup_complete']
    assert not (root / 'runs/gvisor' / identity / 'filesystem-ready.json').exists()
    assert saved.verify()['status'] == 'passed'
    policy.write_text('{}')


def test_concurrent_delayed_restores_and_unrelated_commands(delayed_worker, tmp_path):
    root, policy, calls, saved = delayed_worker
    calls.write_text('')
    policy.write_text(json.dumps({'staging': 1, 'unpacking': 2}))
    samples = []
    try:
        with Sandbox(memory=Memory('128MiB', '256MiB')) as control, ExitStack() as stack:
            with ThreadPoolExecutor(2) as executor:
                start = time.monotonic()
                futures = [executor.submit(Sandbox, snapshot=saved, startup_timeout=60) for _ in range(2)]
                while not all(f.done() for f in futures):
                    usage = shutil.disk_usage(root)
                    assert usage.free > usage.total * .15
                    tick = time.monotonic()
                    assert control.run('printf alive', timeout=5, check=True).stdout == 'alive'
                    samples.append(time.monotonic() - tick)
                    time.sleep(.1)
                envs, errors = [], []
                for future in futures:
                    try:
                        envs.append(stack.enter_context(future.result()))
                    except Exception as error:
                        errors.append(error)
                if errors:
                    raise errors[0]
                elapsed = time.monotonic() - start
            for env in envs:
                for directory, value in (('/workspace', 'root'), ('/var/lib/docker', 'docker'),
                                         ('/var/lib/containerd', 'containerd')):
                    assert env.files.read_text(directory + '/record') == value + '\n'
                assert env.run('stat -c %s /var/lib/docker/data', check=True).stdout == '33554432\n'
                assert (root / 'runs/gvisor' / env.id / 'filesystem-ready.json').is_file()
            assert len([json.loads(line) for line in calls.read_text().splitlines()
                        if json.loads(line)['phase'] == 'unpacking']) == 4
            assert elapsed >= 5 and samples and max(samples) < 5
            report = {'concurrent_restores': 2, 'staging_delay_seconds': 1,
                      'delay_per_mount_seconds': 2, 'startup_timeout_seconds': 60,
                      'restore_seconds': elapsed, 'unrelated_commands': len(samples),
                      'maximum_command_seconds': max(samples),
                      'median_command_seconds': sorted(samples)[len(samples) // 2]}
            (tmp_path / 'restore-deadline.json').write_text(json.dumps(report, indent=2))
            print(json.dumps(report))
    finally:
        policy.write_text('{}')

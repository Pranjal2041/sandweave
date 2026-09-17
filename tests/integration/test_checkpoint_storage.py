"""Save and restore self-bound directories and independent Docker data mounts."""
from concurrent.futures import ThreadPoolExecutor
import os
import time

import pytest

from sandweave import Sandbox
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit disposable worker required')]


@pytest.fixture(scope='module', autouse=True)
def stop_owned_worker():
    yield
    connection = local_connection()
    try:
        connection.call('_shutdown_if_idle')
    finally:
        connection.close()


@pytest.mark.parametrize('storage', [False, True])
def test_self_bind_filesystem_round_trips(storage, tmp_path):
    recipe = {'extends': 'coding', 'runtime_options': {'docker_data': storage}}
    with Sandbox(template=recipe) as source:
        root = '/var/lib/docker' if storage else '/workspace'
        source.run(f'mkdir -p {root}/bound; echo saved > {root}/bound/value; '
                   f'mount --bind {root} {root}; mount --bind {root}/bound {root}/bound', check=True)
        source.run('mkdir -p /var/lib/containerd; echo containerd > /var/lib/containerd/value', check=True)
        (tmp_path / 'mountinfo.txt').write_text(source.run('cat /proc/1/mountinfo', check=True).stdout)
        saved = source.snapshot(state='filesystem')
        source.files.write_text(root + '/bound/value', 'changed')
        with Sandbox(snapshot=saved) as first:
            assert first.files.read_text(root + '/bound/value') == 'saved\n'
            assert first.files.read_text('/var/lib/containerd/value') == 'containerd\n'
            first.files.write_text(root + '/bound/value', 'second')
            second = first.snapshot(state='filesystem')
        with Sandbox(snapshot=second) as restored:
            assert restored.files.read_text(root + '/bound/value') == 'second'
            assert restored.files.read_text('/var/lib/containerd/value') == 'containerd\n'
        assert source.files.read_text(root + '/bound/value') == 'changed'


def test_concurrent_captures_leave_unrelated_commands_responsive():
    recipe = {'extends': 'coding', 'runtime_options': {'docker_data': True}}
    with Sandbox(template=recipe) as first, Sandbox(template=recipe) as second, Sandbox() as unrelated:
        for env in (first, second):
            env.run('dd if=/dev/zero of=/var/lib/docker/data bs=1M count=16; '
                    'mount --bind /var/lib/docker /var/lib/docker', check=True)
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = [executor.submit(env.snapshot, state='filesystem') for env in (first, second)]
            durations = []
            while not all(future.done() for future in pending):
                start = time.monotonic()
                assert unrelated.run('echo responsive', timeout=5, check=True).stdout == 'responsive\n'
                durations.append(time.monotonic() - start)
                time.sleep(.05)
            saved = [future.result() for future in pending]
        assert durations and max(durations) < 5
        for snapshot in saved:
            with Sandbox(snapshot=snapshot) as restored:
                assert restored.run('stat -c %s /var/lib/docker/data', check=True).stdout == '16777216\n'
        print({'unrelated_commands': len(durations), 'maximum_seconds': max(durations)})

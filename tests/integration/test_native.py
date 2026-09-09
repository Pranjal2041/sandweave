"""Actual root-mapped Apptainer execution and filesystem/lifecycle acceptance."""
import os
import uuid

import pytest

from sandweave import Sandbox, CPU, UnsupportedFeature
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit worker required')]


@pytest.fixture(scope='module', autouse=True)
def release_idle_test_worker():
    yield
    connection = local_connection()
    connection.call('_shutdown_if_idle')
    connection.close()


def test_native_commands_pause_files_and_independent_cache(tmp_path):
    setup = tmp_path / 'setup.sh'
    setup.write_text('mkdir -p /opt/example\necho prepared > /opt/example/value\n')
    with Sandbox(runtime='apptainer', setup=setup, cpu=2) as env:
        assert env.run("python -c 'print(2 + 2)'").stdout == '4\n'
        assert env.run('test ! -e /dev/kvm').returncode == 0
        assert env.run("python -c 'import os; print(len(os.sched_getaffinity(0)))'").stdout == '2\n'
        assert 'host network' in env.status()['runtime_status']['runtime']['network']
        assert env.files.read_text('/opt/example/value') == 'prepared\n'
        env.files.write_text('/workspace/unicode', 'hello € 日本語')
        process = env.exec("python -u -c 'print(input())'")
        env.pause()
        assert env.status()['runtime_status']['status'] == 'paused'
        env.resume()
        process.stdin.write('resumed\n'); process.stdin.close()
        assert process.stdout.read() == 'resumed\n'
        assert process.wait(timeout=5) == 0
        with pytest.raises(UnsupportedFeature):
            env.snapshot(state='memory')
        baseline = env.cache('native-' + uuid.uuid4().hex)
        assert baseline.verify()['status'] == 'passed'
        with Sandbox(cache=baseline) as clone:
            assert clone.files.read_text('/workspace/unicode') == 'hello € 日本語'
            clone.files.write_text('/workspace/unicode', 'changed')
            assert env.files.read_text('/workspace/unicode') == 'hello € 日本語'
            clone.run('rm /opt/example/value')
            saved = clone.stop()
        with Sandbox(cache=saved) as restored:
            assert restored.run('test ! -e /opt/example/value').returncode == 0
            assert restored.files.read_text('/workspace/unicode') == 'changed'
        saved = env.stop()
        assert env.status()['state'] == 'stopped'


def test_native_explicit_unsupported_guarantees():
    for options in ({'network': 'offline'}, {'cpu': CPU(weight=50)}, {'template': 'gnome'}):
        with pytest.raises(UnsupportedFeature):
            Sandbox(runtime='apptainer', **options)

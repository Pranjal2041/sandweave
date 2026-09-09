"""Verify actual worker binds, write protection and explicit shared-state policy."""
import os

import pytest

from sandweave import Sandbox, Mount, UnsupportedFeature
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit worker required')]


@pytest.fixture(scope='module', autouse=True)
def release_idle_test_worker():
    yield
    connection = local_connection()
    connection.call('_shutdown_if_idle')
    connection.close()


@pytest.mark.parametrize('runtime', ['gvisor', 'apptainer'])
def test_read_only_mount_rebind_and_remove(runtime, tmp_path):
    (tmp_path / 'value').write_text('dataset')
    with Sandbox(runtime=runtime, mounts=[Mount(tmp_path, '/dataset')]) as env:
        assert env.files.read_text('/dataset/value') == 'dataset'
        assert env.run('echo bad > /dataset/value', check=False).returncode != 0
        env.files.write_text('/workspace/value', 'saved')
        saved = env.cache('mount-ro-' + env.id)
        assert saved.dependencies['external_mounts'][0]['read_only'] is True
    (tmp_path / 'value').write_text('external update')
    with Sandbox(cache=saved) as env:
        assert env.files.read_text('/dataset/value') == 'external update'
        assert env.files.read_text('/workspace/value') == 'saved'
    with Sandbox(cache=saved, mounts=[]) as env:
        assert env.run('test ! -f /dataset/value').returncode == 0


@pytest.mark.parametrize('runtime', ['gvisor', 'apptainer'])
def test_external_write_requires_capture_policy(runtime, tmp_path):
    with Sandbox(runtime=runtime, mounts=[Mount(tmp_path, '/results', read_only=False)]) as env:
        env.files.write_text('/results/value', 'external')
        assert (tmp_path / 'value').read_text() == 'external'
        with pytest.raises(UnsupportedFeature):
            env.stop()
        assert env.run('echo alive').stdout == 'alive\n'
    with Sandbox(runtime=runtime, mounts=[Mount(tmp_path, '/results', read_only=False, snapshot='rebind')]) as env:
        saved = env.cache('mount-rw-' + env.id)
        env.files.write_text('/results/value', 'new state')
    with Sandbox(cache=saved) as env:
        assert env.files.read_text('/results/value') == 'new state'

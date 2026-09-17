"""Public Docker template: empty storage, real daemon data and optional imports."""
import io
import os
from pathlib import Path
import tarfile
import uuid

import pytest

from sandweave import Sandbox
from sandweave.sandbox.targets import local_connection
from sandweave.sandbox import workspace

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


def assert_storage(env):
    assert env.run('stat -f -c %T /var/lib/docker /var/lib/containerd', check=True).stdout == 'tmpfs\ntmpfs\n'
    env.run('docker info; ctr version', check=True)


@pytest.mark.parametrize('storage', [True, False])
def test_empty_docker_storage_and_cold_restore(storage, tmp_path):
    recipe = 'docker' if storage else {'extends': 'docker', 'runtime_options': {'docker_data': False}}
    if not storage:
        # Disabling the separate filesystems is meaningful for a custom
        # storage driver. overlay2 itself cannot use an overlay-backed upper.
        setup = tmp_path / 'vfs.sh'
        setup.write_text('set -eu\nsystemctl stop docker.socket docker.service\n'
                         'printf \'{"storage-driver":"vfs"}\\n\' > /etc/docker/daemon.json\n'
                         'systemctl reset-failed docker.service\n')
        recipe['setup'] = {'script': str(setup)}
    with Sandbox(template=recipe) as source:
        if storage:
            assert_storage(source)
        else:
            assert 'tmpfs' not in source.run('stat -f -c %T /var/lib/docker /var/lib/containerd', check=True).stdout
        assert source.run('docker image ls -q', check=True).stdout == ''
        saved = source.snapshot(state='filesystem')
        with Sandbox(snapshot=saved) as restored:
            if storage:
                assert_storage(restored)
            else:
                assert 'tmpfs' not in restored.run('stat -f -c %T /var/lib/docker /var/lib/containerd', check=True).stdout
            assert restored.run('docker image ls -q', check=True).stdout == ''
            assert restored.run('docker ps -aq', check=True).stdout == ''


def assert_populated(env, image_id):
    assert_storage(env)
    assert env.run('docker image inspect --format "{{.Id}}" busybox:1.37.0', check=True).stdout.strip() == image_id
    assert 'storage-check' in env.run('ctr namespaces list -q', check=True).stdout.splitlines()
    env.run('docker start checkpoint', timeout=30, check=True)
    assert env.run('docker exec checkpoint cat /writable', check=True).stdout == 'container-file\n'
    assert env.run('docker exec checkpoint cat /data/record', check=True).stdout == 'volume-file\n'
    assert env.run('docker run --pull=never --rm --network none busybox:1.37.0 echo image-restored',
                   timeout=30, check=True).stdout == 'image-restored\n'
    assert env.files.read_text('/var/lib/containerd/checkpoint-marker') == 'containerd-file'


@pytest.mark.parametrize('state', ['filesystem', 'memory'])
def test_docker_images_containers_volumes_and_containerd_survive(state, tmp_path):
    with Sandbox(template='docker') as source:
        assert_storage(source)
        source.run('docker pull busybox:1.37.0', timeout=180, check=True)
        image_id = source.run('docker image inspect --format "{{.Id}}" busybox:1.37.0', check=True).stdout.strip()
        source.run('ctr namespaces create storage-check', check=True)
        source.run('docker run -d --name checkpoint -v record:/data busybox:1.37.0 sleep infinity',
                   timeout=60, check=True)
        source.run("docker exec checkpoint sh -c 'echo container-file > /writable; echo volume-file > /data/record'", check=True)
        source.files.write_text('/var/lib/containerd/checkpoint-marker', 'containerd-file')
        (tmp_path / 'mountinfo.txt').write_text(source.run('cat /proc/1/mountinfo', check=True).stdout)
        saved = source.snapshot(state=state)
        source.run("docker exec checkpoint sh -c 'echo changed > /data/record'", check=True)
        source.terminate()
        with Sandbox(snapshot=saved) as restored:
            if state == 'memory':
                assert restored.run('docker inspect --format "{{.State.Running}}" checkpoint', check=True).stdout == 'true\n'
            assert_populated(restored, image_id)
            # Re-saving a cold restore must not stack or lose its storage mounts.
            again = restored.snapshot(state='filesystem')
        with Sandbox(snapshot=again) as restored:
            assert_populated(restored, image_id)


def test_archive_import_is_optional_and_not_a_restore_dependency():
    # Install the ordinary template first, then provide a test-owned immutable
    # archive through the same runtime_options used by custom templates.
    with Sandbox(template='docker'):
        pass
    name = 'storage-import-' + uuid.uuid4().hex + '.tar'
    relative = Path('images') / name
    archive = workspace.assets() / relative
    with tarfile.open(archive, 'w') as output:
        for directory in ('docker', 'containerd'):
            data = ('imported-' + directory).encode()
            entry = tarfile.TarInfo(directory + '/import-marker')
            entry.size, entry.mode = len(data), 0o600
            output.addfile(entry, io.BytesIO(data))
    try:
        recipe = {'extends': 'docker', 'runtime_options': {'docker_archive': str(relative)}}
        with Sandbox(template=recipe) as source:
            assert_storage(source)
            for directory in ('docker', 'containerd'):
                assert source.files.read_text('/var/lib/' + directory + '/import-marker') == 'imported-' + directory
            source.files.write_text('/var/lib/docker/import-marker', 'saved-after-import')
            saved = source.snapshot(state='filesystem')
        archive.unlink()
        for staged in (workspace.home() / 'workers').glob('*/*/images/' + name):
            staged.unlink()
        with Sandbox(snapshot=saved) as restored:
            assert_storage(restored)
            assert restored.files.read_text('/var/lib/docker/import-marker') == 'saved-after-import'
            assert restored.files.read_text('/var/lib/containerd/import-marker') == 'imported-containerd'
    finally:
        archive.unlink(missing_ok=True)

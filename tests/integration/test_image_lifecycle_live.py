"""Public local-image import, concurrent output, and durable private-file deletion."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import time

import pytest

from sandweave import Sandbox, Memory, Mount
from sandweave.sandbox.targets import local_connection
from oci_fixture import archive

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit disposable worker required')]


def drain(connection):
    deadline = time.monotonic() + 90
    while True:
        try:
            connection.call('_shutdown_if_idle')
            return
        except RuntimeError as error:
            if 'pending deletions' not in str(error) or time.monotonic() >= deadline:
                raise
            time.sleep(.1)


def test_local_archive_parallel_commands_delete_and_snapshot_restore(tmp_path):
    source = archive(tmp_path/'busybox.oci.tar')
    saved = Sandbox.import_image(source, timeout=600)
    env = Sandbox(snapshot=saved, memory=Memory('256MiB', '256MiB'))
    peer = Sandbox(memory=Memory('256MiB', '256MiB'))
    connection = local_connection()
    try:
        result = env.run('id -u; pwd; echo "$LOCAL_IMAGE"')
        assert result.stdout == '123\n/local-image\nyes\n'
        env.files.write_text('/local-image/kept', 'checkpoint survives deletion')
        checkpoint = env.snapshot(state='filesystem')
        def command(index):
            result = env.run('printf out-' + str(index) + '; printf err-' + str(index) + ' >&2')
            assert (result.stdout, result.stderr, result.returncode) == (f'out-{index}', f'err-{index}', 0)
        with ThreadPoolExecutor(32) as executor:
            list(executor.map(command, range(256)))
        workspace = Path(connection.call('ping')['workspace'])
        local = Path((workspace/'runs/local-path.txt').read_text().strip())
        assert (local/'gvisor/bundles'/env.id).exists()
        assert env.delete(wait=False)['state'] in ('pending', 'deleted')
        env.close()
        deadline = time.monotonic() + 60
        while connection.call('delete_status', identity=env.id)['state'] != 'deleted':
            assert time.monotonic() < deadline
            assert peer.run('printf responsive').stdout == 'responsive'
            time.sleep(.05)
        assert not (local/'gvisor/bundles'/env.id).exists()
        assert not (workspace/'runs/gvisor'/env.id).exists()
        assert checkpoint.verify()['status'] == 'passed'
        with Sandbox(snapshot=checkpoint, memory=Memory('256MiB', '256MiB')) as restored:
            assert restored.files.read_text('/local-image/kept') == 'checkpoint survives deletion'
    finally:
        for item in (env, peer):
            item.delete()
            item.close()
        drain(connection)
        connection.close()


def test_delete_preserves_external_mounts_and_cleans_native_runtime(tmp_path):
    external = tmp_path/'external'
    external.mkdir()
    (external/'keep').write_text('external data')
    env = Sandbox(runtime='apptainer', mounts=[Mount(str(external), '/shared', snapshot='rebind')])
    connection = local_connection()
    try:
        assert env.files.read_text('/shared/keep') == 'external data'
        assert env.delete()['state'] == 'deleted'
        assert (external/'keep').read_text() == 'external data'
        assert env.status()['deleted']
    finally:
        env.terminate()
        env.close()
        drain(connection)
        connection.close()

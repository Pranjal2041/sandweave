"""Import a client archive through Weave, then restore it on another worker."""
import os

import pytest

from sandweave import Sandbox, Memory, Pool
from oci_fixture import archive
from test_weave_live import cluster, wait_for

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_WEAVE_INTEGRATION'), reason='explicit isolated workers required')]


def test_client_archive_transfer_pool_and_controller_owned_delete(cluster, tmp_path):
    source = archive(tmp_path/'client-only.oci.tar')
    saved = Sandbox.import_image(source, target='weave-live', timeout=600)
    source.unlink()  # Subsequent placements cannot consult the client's file.
    wait_for(lambda: all(a.get('deleted') for a in cluster.info['sandboxes']))
    builder = next(a for a in cluster.info['sandboxes'] if a.get('deleted'))
    cluster.drain(builder['worker'])
    memory = Memory('256MiB', '256MiB')
    with Pool(target='weave-live', snapshot=saved, size=1, warm=1, memory=memory,
              wait_timeout=120, startup_timeout=120) as pool:
        with pool.acquire() as env:
            assert env.run('id -u; echo "$LOCAL_IMAGE"').stdout == '123\nyes\n'
            worker = cluster.connection.call('allocation_get', identity=env.id)['worker']
            assert worker != builder['worker']
    env = Sandbox(target='weave-live', snapshot=saved, memory=memory)
    identity = env.id
    env.delete(wait=False)
    env.close()
    wait_for(lambda: cluster.connection.call('allocation_get', identity=identity).get('deleted'))
    assert saved.verify()['status'] == 'passed'

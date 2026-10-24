"""Guest clocks through authenticated Weave, direct connections and pools."""
from concurrent.futures import ThreadPoolExecutor
import os
import time

import pytest

from sandweave import Memory, Pool, Sandbox
from sandweave.weave.transport import join_link
from test_guest_clock_live import DAY, SLACK, assert_offset, probe
from test_weave_live import cluster

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_WEAVE_INTEGRATION'), reason='explicit isolated workers required')]
MEMORY = Memory('256MiB', '512MiB')


def test_weave_forwarded_direct_and_pool_clocks(cluster):
    target = join_link(cluster.info['connection']['address'], cluster.connection.token)
    offset = -45 * DAY
    env = Sandbox(target=target, clock_offset=offset, memory=MEMORY)
    try:
        assert_offset(probe(env), offset)
        assert env.info['clock'] == {'offset_seconds': offset, 'pause': 'freeze'}
        borrowed = Sandbox.connect(env.id, target=target, connection='direct')
        try:
            first = probe(borrowed)
            env.pause()
            time.sleep(3)
            env.resume()
            second = probe(borrowed)
            host = second['host_before'] - first['host_after']
            assert host >= 3
            assert second['syscall_realtime'] - first['syscall_realtime'] < host - 3 + SLACK
        finally:
            borrowed.close()
    finally:
        env.terminate()
        env.close()
    with Pool(target=target, size=2, warm=2, clock_offset=offset, memory=MEMORY) as pool:
        def exercise(index):
            with pool.acquire() as member:
                assert_offset(probe(member), offset)
                return member.id
        with ThreadPoolExecutor(2) as executor:
            assert len(set(executor.map(exercise, range(2)))) == 2

"""Audit export through authenticated Weave, direct connections and pools."""
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from sandweave import Sandbox, Pool, Memory
from sandweave.weave.transport import join_link
from test_weave_live import cluster

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_WEAVE_INTEGRATION'), reason='explicit isolated workers required')]


def test_weave_direct_and_pool_audit(cluster, tmp_path):
    target = join_link(cluster.info['connection']['address'], cluster.connection.token)
    env = Sandbox(target=target, audit=True, hidepid=2, memory=Memory('256MiB', '512MiB'))
    try:
        borrowed = Sandbox.connect(env.id, target=target, connection='direct')
        try:
            assert borrowed.run('printf remote').stdout == 'remote'
            assert env.audit_log(tmp_path/'forwarded.jsonl')['complete']
            assert asyncio.run(borrowed.audit_log.aio(tmp_path/'direct.jsonl'))['complete']
        finally:
            borrowed.close()
    finally:
        env.terminate()
        env.close()
    pool = Pool(target=target, size=2, warm=0, audit=True, hidepid=2,
                memory=Memory('256MiB','512MiB'))
    pool.start()
    try:
        def exercise(index):
            with pool.acquire() as member:
                assert 'hidepid=2' in member.run('cat /proc/mounts').stdout
                assert member.audit_log(tmp_path/('pool-%d.jsonl'%index))['complete']
                return member.id
        with ThreadPoolExecutor(2) as executor:
            assert len(set(executor.map(exercise, range(2)))) == 2
    finally:
        pool.close()

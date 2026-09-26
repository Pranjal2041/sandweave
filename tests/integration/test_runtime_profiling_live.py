"""Heap capture through public remote handles and memory restores."""
import asyncio
import gzip
import os

import pytest

from sandweave import Sandbox, Memory
from sandweave.weave.transport import join_link
from test_weave_live import cluster

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_WEAVE_INTEGRATION'), reason='explicit isolated workers required')]


def test_weave_heap_capture_and_memory_restore(cluster, tmp_path):
    target = join_link(cluster.info['connection']['address'], cluster.connection.token)
    with Sandbox(target=target, profiling=True, memory=Memory('256MiB', '256MiB')) as env:
        assert env.run('printf original').stdout == 'original'
        borrowed = Sandbox.connect(env.id, target=target)
        try:
            borrowed.profile(tmp_path / 'remote.pprof')
            asyncio.run(borrowed.profile.aio(tmp_path / 'async.pprof'))
        finally:
            borrowed.close()
        saved = env.snapshot(state='memory')
        with Sandbox(target=target, snapshot=saved) as restored:
            assert restored.run('printf restored').stdout == 'restored'
            assert restored.spec['template']['runtime_options']['profile'] is True
            restored.profile(tmp_path / 'restored.pprof')
    for name in ('remote', 'async', 'restored'):
        assert gzip.decompress((tmp_path / (name + '.pprof')).read_bytes())

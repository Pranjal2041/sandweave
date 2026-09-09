"""Run only on the explicitly configured disposable acceptance worker."""
import os
import time
import uuid

import pytest

from sandweave.sandbox.resources import normalize
from sandweave.sandbox.workspace import prepare
from sandweave.sandbox.runtimes.gvisor.driver import Runtime
from sandweave.templates import Template

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit worker required')]


def test_real_guest_command_and_cleanup():
    runtime = Runtime(prepare())
    identity = 'sw-smoke-' + uuid.uuid4().hex[:10]
    spec = {'template': Template().resolve(), 'resources': normalize(), 'startup_timeout': 90}
    try:
        metadata = runtime.create(identity, spec)
        agent = runtime.agent(identity, metadata)
        process = 'process-' + uuid.uuid4().hex
        agent.call('spawn', identity=process, argv=['/bin/sh', '-c',
                   "test ! -e /dev/kvm && python -c 'print(2 + 2)'"], timeout=10)
        deadline = time.monotonic() + 15
        while (state := agent.call('status', identity=process))['returncode'] is None:
            assert time.monotonic() < deadline
            time.sleep(.02)
        assert state['returncode'] == 0
        assert agent.call('output', identity=process, stream='stdout') == b'4\n'
    finally:
        if runtime.status(identity)['status'] in ('running', 'paused', 'starting'):
            runtime.terminate(identity)

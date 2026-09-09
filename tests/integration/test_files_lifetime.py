"""Real descriptors, worker-owned expiry and resource reservation failures."""
import os
import time
import uuid

import pytest

from sandweave import Sandbox, ResourceUnavailable
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
def test_streams_are_real_guest_descriptors(runtime):
    with Sandbox(runtime=runtime) as env:
        text = 'hello € 日本語\n' * 100000
        with env.files.open('/workspace/text', 'w') as stream:
            assert stream.write(text) == len(text)
        with env.files.open('/workspace/text') as stream:
            env.run('mv /workspace/text /workspace/renamed')
            assert stream.readline() == 'hello € 日本語\n'
            stream.seek(0)
            assert stream.read() == text
        with pytest.raises(FileExistsError):
            env.files.open('/workspace/renamed', 'x')
        with env.files.open('/workspace/renamed', 'r+b') as stream:
            stream.seek(0); stream.write(b'HELLO'); stream.flush()
            stream.seek(0); assert stream.read(5) == b'HELLO'
            stream.truncate(5)
        assert env.files.read_bytes('/workspace/renamed') == b'HELLO'
        with env.files.open('/workspace/renamed', 'ab') as stream:
            stream.write(b' again')
        assert env.files.read_bytes('/workspace/renamed') == b'HELLO again'


def test_ttl_survives_client_disconnect_and_pause():
    env = Sandbox(ttl=1.5)
    identity = env.id
    env.exec('sleep 1000')
    env.pause()
    env.close()
    deadline = time.monotonic() + 8
    with Sandbox.connect(identity) as borrowed:
        while borrowed.status()['state'] != 'terminated':
            assert time.monotonic() < deadline
            time.sleep(.1)
        assert borrowed.status()['termination_reason'] == 'ttl'
        assert borrowed.status()['runtime_status']['status'] == 'stopped'


def test_admission_and_unique_live_names():
    with pytest.raises(ResourceUnavailable):
        Sandbox(memory='4096TiB')
    name = 'unique-' + uuid.uuid4().hex
    with Sandbox(name=name) as env:
        with pytest.raises(FileExistsError):
            Sandbox(name=name)
        with Sandbox.connect(name) as same:
            assert same.id == env.id
    with Sandbox(name=name):
        pass

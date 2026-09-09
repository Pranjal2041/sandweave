"""Real descriptors, worker-owned expiry and resource reservation failures."""
import os
import time
import uuid

import pytest

from sandweave import Sandbox, ResourceUnavailable, OutputLimitExceeded
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


def test_idle_connection_and_bounded_output():
    with Sandbox() as env:
        env.files.write_text('/workspace/idle', 'before')
        time.sleep(65)  # Previously exceeded the guest HTTP keepalive timeout.
        assert env.files.read_text('/workspace/idle') == 'before'
        with pytest.raises(OutputLimitExceeded) as failed:
            env.run("python -c 'import os; exec(\"while True: os.write(1,b*x)\".replace(\"b*x\",\"bytes(65536)\"))'",
                    max_output_bytes=2*1024**2, timeout=10)
        assert failed.value.result.output_limited
        assert len(failed.value.stdout) <= 1024**2
        assert env.run('echo alive').stdout == 'alive\n'


def test_directory_transfer_and_open_handle_ram_restore(tmp_path):
    from sandweave.sandbox.files import RemoteFile
    source = tmp_path / 'source'
    (source / 'empty').mkdir(parents=True)
    (source / 'value').write_text('abcdefgh')
    with Sandbox() as env:
        env.files.upload(source, '/workspace/tree')
        destination = env.files.download('/workspace/tree', tmp_path / 'copy')
        assert (destination / 'empty').is_dir()
        assert (destination / 'value').read_text() == 'abcdefgh'
        stream = RemoteFile(env, '/workspace/tree/value', 'rb')
        stream.seek(3)
        env.run('rm /workspace/tree/value')
        saved = env.snapshot(state='memory')
        with Sandbox(snapshot=saved) as clone:
            assert clone._call('file', op='read', handle=stream.handle, size=5) == b'defgh'
            clone._call('file', op='close', handle=stream.handle)
        stream.close()

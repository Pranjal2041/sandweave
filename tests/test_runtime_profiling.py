import asyncio
import gzip
from types import SimpleNamespace

import pytest

from sandweave import Sandbox
from sandweave.cli import creation, parser
from sandweave.sandbox.errors import UnsupportedFeature
from sandweave.sandbox.runtimes.gvisor.driver import Runtime
from sandweave.sandbox.sandbox import definition
from sandweave.sandbox.worker import Worker


def test_profiling_is_explicit_and_reaches_engine(tmp_path):
    runtime = Runtime.__new__(Runtime)
    runtime.root = tmp_path
    assert '--profile' not in runtime.options(definition()['spec'])
    request = definition(profiling=True)
    assert request['spec']['template']['runtime_options']['profile'] is True
    assert '--profile' in runtime.options(request['spec'])
    assert '--profile' not in runtime.options(definition(
        template={'extends': 'coding', 'runtime_options': {'profile': True}}, profiling=False)['spec'])
    assert creation(parser().parse_args(['create', '--profiling']))['profiling'] is True


@pytest.mark.parametrize('value', ['true', 1, {}])
def test_invalid_profiling_request(value):
    with pytest.raises(ValueError, match='profiling'):
        definition(profiling=value)


def test_native_runtime_does_not_silently_ignore_profiling():
    with pytest.raises(UnsupportedFeature, match='gvisor'):
        definition(profiling=True, runtime='apptainer')


def test_profile_does_not_call_guest_or_hold_lifecycle_lock():
    import threading
    lock = threading.RLock()
    worker = Worker.__new__(Worker)
    worker.lock = lambda identity: lock
    worker.read = lambda identity: {'spec': definition(profiling=True)['spec']}
    payload = gzip.compress(b'profile')
    def capture(identity):
        assert not lock._is_owned()
        return payload
    worker.runtime = SimpleNamespace(adapter=lambda name: SimpleNamespace(profile=capture))
    assert worker.dispatch('runtime_profile', {'identity': 'example'}) == payload
    worker.read = lambda identity: {'spec': definition()['spec']}
    with pytest.raises(UnsupportedFeature, match='profiling=True'):
        worker.dispatch('runtime_profile', {'identity': 'example'})


def test_sync_and_async_profile_save_on_client(tmp_path):
    payload = gzip.compress(b'profile')
    env = Sandbox.__new__(Sandbox)
    env._call = lambda op: payload
    async def acall(op):
        return payload
    env._acall = acall
    path = tmp_path / 'sync.pprof'
    assert env.profile(path) == path
    assert path.read_bytes() == payload
    async def collect():
        path = tmp_path / 'async.pprof'
        assert await env.profile.aio(path) == path
        assert path.read_bytes() == payload
    asyncio.run(collect())


def test_capture_uses_runtime_socket_and_preserves_profile_bytes(monkeypatch, tmp_path):
    runtime = Runtime.__new__(Runtime)
    runtime.root = tmp_path
    (tmp_path/'runs').mkdir()
    runtime.manager = SimpleNamespace(_command=lambda identity, **kw: ['runsc', '--root=/state'])
    payload = gzip.compress(b'profile')
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        output = next(arg.removeprefix('--output=/lab/') for arg in command if arg.startswith('--output='))
        (tmp_path/output).write_bytes(payload)
        return SimpleNamespace(returncode=0, stdout=b'Collecting heap', stderr=b'')
    monkeypatch.setattr('sandweave.sandbox.runtimes.gvisor.driver.subprocess.run', run)
    assert runtime.profile('example') == payload
    assert calls[0][0][:4] == ['runsc', '--root=/state', 'profile', 'heap']
    assert calls[0][0][-1] == 'example'
    assert not list((tmp_path/'runs').iterdir())
    assert calls[0][1]['timeout'] == 30


def test_heap_capture_is_scoped_to_its_sandbox():
    from sandweave.sandbox.management import Management
    manager = Management.__new__(Management)
    manager.read = lambda identity: {'id': 's1', 'token': 'secret'} if identity == 's1' else None
    assert manager.authorize('sw1.s1.secret', 'runtime_profile', {'identity': 's1'})
    assert not manager.authorize('sw1.s1.secret', 'runtime_profile', {'identity': 's2'})
    assert not manager.authorize('sw1.s1.other', 'runtime_profile', {'identity': 's1'})


@pytest.mark.parametrize('failure', ['command', 'timeout', 'invalid'])
def test_failed_capture_removes_temporary_profile(monkeypatch, tmp_path, failure):
    import subprocess
    runtime = Runtime.__new__(Runtime)
    runtime.root = tmp_path
    (tmp_path/'runs').mkdir()
    runtime.manager = SimpleNamespace(_command=lambda identity, **kw: ['runsc'])
    def run(command, **kwargs):
        output = next(arg.removeprefix('--output=/lab/') for arg in command if arg.startswith('--output='))
        (tmp_path/output).write_bytes(b'partial diagnostic data')
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(command, 30)
        return SimpleNamespace(returncode=int(failure == 'command'), stdout=b'', stderr=b'capture failed')
    monkeypatch.setattr('sandweave.sandbox.runtimes.gvisor.driver.subprocess.run', run)
    with pytest.raises((RuntimeError, subprocess.TimeoutExpired)):
        runtime.profile('example')
    assert not list((tmp_path/'runs').iterdir())

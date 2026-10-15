import asyncio
import json
import threading
from types import SimpleNamespace
from pathlib import Path

import pytest

from sandweave import Sandbox
from sandweave.cli import creation, parser
from sandweave.sandbox.audit import check_worker
from sandweave.sandbox.errors import UnsupportedFeature
from sandweave.sandbox.runtimes.gvisor.driver import Runtime
from sandweave.sandbox.sandbox import definition
from sandweave.sandbox.worker import Worker


def test_flags_default_off_and_template_overrides(tmp_path):
    runtime = Runtime.__new__(Runtime)
    runtime.root = tmp_path
    original = definition()['spec']
    assert not original['template']['runtime_options'].get('audit')
    assert not original['template']['runtime_options'].get('hidepid')
    request = definition(hidepid=2, audit=True)
    options = runtime.options(request['spec'])
    assert options[options.index('--hidepid') + 1] == '2'
    assert '--audit' in options
    off = definition(template={'extends': 'coding', 'runtime_options': {'hidepid': 2, 'audit': True}}, hidepid=0, audit=False)
    assert '--no-audit' in runtime.options(off['spec'])
    cli = creation(parser().parse_args(['create', '--hidepid', '2', '--audit']))
    assert cli['hidepid'] == 2 and cli['audit'] is True
    assert creation(parser().parse_args(['create', '--no-audit']))['audit'] is False


@pytest.mark.parametrize('options', [{'hidepid': True}, {'hidepid': 3}, {'hidepid': '2'}, {'audit': 1}, {'audit': 'true'}])
def test_invalid_flag_values(options):
    with pytest.raises(ValueError):
        definition(**options)


@pytest.mark.parametrize('options', [{'hidepid': 2}, {'audit': True}])
def test_flags_cannot_be_silently_ignored_by_older_worker_or_other_runtime(options):
    with pytest.raises(UnsupportedFeature):
        definition(runtime='apptainer', **options)
    with pytest.raises(UnsupportedFeature):
        check_worker(definition(**options)['spec'], SimpleNamespace(call=lambda op: {}))


def test_export_chunks_and_preserves_incomplete_status(tmp_path):
    payload = b'{"sequence":1}\n' * 100000
    env = Sandbox.__new__(Sandbox)
    def call(op, offset=None, size=None):
        assert op == 'runtime_audit'
        if offset is None:
            return {'bytes': len(payload), 'complete': False, 'dropped': 2}
        assert size <= 1024**2
        return payload[offset:offset+size]
    env._call = call
    async def acall(*args, **kwargs):
        return call(*args, **kwargs)
    env._acall = acall
    path = tmp_path / 'sync.jsonl'
    assert env.audit_log(path)['complete'] is False
    assert path.read_bytes() == payload
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads((tmp_path / 'sync.jsonl.status.json').read_text())['dropped'] == 2
    async def run():
        assert not (await env.audit_log.aio(tmp_path / 'async.jsonl'))['complete']
    asyncio.run(run())
    assert (tmp_path / 'async.jsonl').read_bytes() == payload


def test_interrupted_export_cannot_leave_clean_status(tmp_path):
    env = Sandbox.__new__(Sandbox)
    env._call = lambda op, **kw: b'' if kw else {'bytes': 10, 'complete': True}
    path = tmp_path / 'audit.jsonl'
    path.with_name(path.name+'.status.json').write_text('{"complete":true}')
    with pytest.raises(RuntimeError):
        env.audit_log(path)
    assert not json.loads(path.with_name(path.name+'.status.json').read_text())['complete']


def test_audit_export_does_not_hold_lifecycle_lock():
    worker = Worker.__new__(Worker)
    lock = threading.RLock()
    worker.lock = lambda identity: lock
    worker.read = lambda identity: {'spec': definition(audit=True)['spec']}
    def audit(identity, **kw):
        assert not lock._is_owned()
        return {'complete': True}
    worker.runtime = SimpleNamespace(adapter=lambda name: SimpleNamespace(audit=audit))
    assert worker.dispatch('runtime_audit', {'identity': 's'})['complete']


def test_audit_authentication_is_scoped():
    from sandweave.sandbox.management import Management
    manager = Management.__new__(Management)
    manager.read = lambda identity: {'id': 's1', 'token': 'secret'} if identity == 's1' else None
    assert manager.authorize('sw1.s1.secret', 'runtime_audit', {'identity': 's1'})
    assert not manager.authorize('sw1.s1.secret', 'runtime_audit', {'identity': 's2'})


def test_requested_audit_fails_closed_and_requires_lifecycle_coverage(monkeypatch, tmp_path):
    import importlib.util
    source = Path(__file__).resolve().parents[1] / 'scripts/audit_log.py'
    module_spec = importlib.util.spec_from_file_location('audit_log', source)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    logs = tmp_path/'runs/gvisor/sandbox'
    logs.mkdir(parents=True)
    metadata = ('Name: sentry/execve, optional fields: [fd_info|binary_sha256], context fields: [time|credentials]\n'
                'Name: syscall/openat/exit, optional fields: [fd_path], context fields: [time|credentials]\n'
                'Name: audit\n')
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout=metadata))
    config = json.loads(module.configure(tmp_path, logs, '/runtime').read_text())
    assert config['fail_on_error'] is True
    assert config['trace_session']['ignore_missing'] is False
    assert config['trace_session']['sinks'][0]['ignore_setup_error'] is False
    assert config['trace_session']['points'][0]['optional_fields'] == []
    metadata = 'Name: audit\n'
    with pytest.raises(RuntimeError, match='coverage'):
        module.configure(tmp_path, logs, '/runtime')

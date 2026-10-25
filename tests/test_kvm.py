import copy
import json
from types import SimpleNamespace

import pytest

from sandweave.cli import creation, parser
from sandweave.sandbox import kvm
from sandweave.sandbox.errors import UnsupportedFeature
from sandweave.sandbox.information import summarize
from sandweave.sandbox.runtimes.gvisor.driver import Runtime
from sandweave.sandbox.sandbox import definition
from sandweave.weave import scheduler


def test_requests_are_validated_and_default_to_systrap():
    assert 'kvm' not in definition()['spec']
    assert 'kvm' not in definition(kvm=False)['spec']
    assert definition(kvm=True)['spec']['kvm'] is True
    assert definition(kvm='auto')['spec']['kvm'] == 'auto'
    for value in ('yes', 1, 'on'):
        with pytest.raises(ValueError, match='kvm must be'):
            definition(kvm=value)
    with pytest.raises(UnsupportedFeature, match='runtime="gvisor"'):
        definition(kvm=True, runtime='apptainer')
    with pytest.raises(UnsupportedFeature, match='GPU'):
        definition(kvm=True, gpu=True)
    assert definition(kvm='auto', gpu=True)['spec']['kvm'] == 'auto'


@pytest.mark.parametrize('source', ['cache', 'snapshot'])
@pytest.mark.parametrize('captured, requested, expected', [
    (True, None, True), (True, False, None), (True, 'auto', 'auto'),
    (False, None, None), (False, True, True), (None, None, None)])
def test_restores_keep_the_snapshot_platform_unless_they_choose(monkeypatch, source, captured, requested, expected):
    saved = definition()['spec']
    if captured is not None:
        saved['kvm'] = captured
    original = copy.deepcopy(saved)

    class Connection:
        def call(self, operation, **kwargs):
            assert operation == 'snapshot_spec'
            return {'reference': 'snap-pinned', 'spec': saved}

        def close(self):
            pass

    monkeypatch.setattr('sandweave.sandbox.sandbox.connect', lambda target: Connection())
    restored = definition(**{source: 'saved'}, **({} if requested is None else {'kvm': requested}))
    assert restored['spec'].get('kvm') == expected
    assert saved == original


@pytest.mark.parametrize('present', [True, False])
def test_workers_resolve_the_platform(monkeypatch, present):
    monkeypatch.setattr(kvm, 'available', lambda: present)
    assert kvm.platform(None) == kvm.platform(False) == 'systrap'
    assert kvm.platform('auto') == ('kvm' if present else 'systrap')
    assert kvm.platform('auto', gpu=True) == 'systrap'
    if present:
        assert kvm.platform(True) == 'kvm'
    else:
        with pytest.raises(UnsupportedFeature, match='/dev/kvm'):
            kvm.platform(True)


def test_launcher_receives_the_resolved_platform(tmp_path, monkeypatch):
    runtime = Runtime.__new__(Runtime)
    runtime.root = tmp_path
    monkeypatch.setattr(kvm, 'available', lambda: True)
    def selected(spec):
        options = runtime.options(spec)
        return options[options.index('--platform') + 1]
    assert selected(definition()['spec']) == 'systrap'
    assert selected(definition(kvm=True)['spec']) == 'kvm'
    assert selected(definition(kvm='auto')['spec']) == 'kvm'
    monkeypatch.setattr(kvm, 'available', lambda: False)
    assert selected(definition(kvm='auto')['spec']) == 'systrap'
    with pytest.raises(UnsupportedFeature):
        selected(definition(kvm=True)['spec'])


def test_snapshots_record_the_platform_they_ran_on(tmp_path, monkeypatch):
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'home'))
    from sandweave.sandbox.snapshots import Store
    store = Store(SimpleNamespace(root=tmp_path / 'workspace'))
    location = tmp_path / 'snapshot'
    location.mkdir()
    (location / 'snapshot-manifest.json').write_text(json.dumps({'snapshot_id': 'lab-snapshot'}))
    source = {'id': 'sw-source', 'spec': definition(kvm='auto')['spec'], 'agent': {'token': 'agent'}}
    for platform, expected in (('kvm', True), ('systrap', False)):
        saved = {'snapshot': str(location), 'kind': 'live', 'platform': platform}
        metadata = store._record('snap-' + platform, saved, source)
        assert metadata['spec']['kvm'] is expected
    assert source['spec']['kvm'] == 'auto'


def test_older_workers_and_controllers_reject_kvm():
    with pytest.raises(UnsupportedFeature, match='0.2.48'):
        kvm.check_worker(definition(kvm=True)['spec'], SimpleNamespace(call=lambda op: {}))
    kvm.check_worker(definition()['spec'], SimpleNamespace(call=lambda op: {}))
    kvm.check_worker(definition(kvm='auto')['spec'], SimpleNamespace(call=lambda op: {'kvm_platform': 1}))


def test_weave_places_required_kvm_only_on_workers_that_have_it():
    workers = [dict(id=name, state='ready', capacity={'slots': 4, 'memory': 100 * 1024**3, 'gpu': 0},
                    inventory={'kvm': has})
               for name, has in (('plain', False), ('older', None), ('kvm', True))]
    def placed(spec):
        requests = [dict(id=f'r{i}', parent=None, spec=spec, created=i) for i in range(6)]
        assignments, waiting = scheduler.plan(requests, workers, [], {})
        return {w for _, w, _ in assignments}, waiting
    workers_used, waiting = placed(definition(kvm=True, detached=True)['spec'])
    assert workers_used == {'kvm'} and len(waiting) == 2
    assert 'KVM is unavailable' in next(iter(waiting.values()))
    assert placed(definition(kvm='auto', detached=True)['spec'])[0] == {'plain', 'older', 'kvm'}
    assert placed(definition(detached=True)['spec'])[0] == {'plain', 'older', 'kvm'}


def test_cli_options():
    assert creation(parser().parse_args(['create', '--kvm']))['kvm'] is True
    assert creation(parser().parse_args(['create', '--kvm-auto']))['kvm'] == 'auto'
    assert creation(parser().parse_args(['create', '--snapshot', 'snap-x', '--no-kvm']))['kvm'] is False
    assert 'kvm' not in creation(parser().parse_args(['create']))
    run = parser().parse_args(['run', '--kvm', 'echo', 'hi'])
    assert creation(run)['kvm'] is True and run.command == ['echo', 'hi']
    with pytest.raises(SystemExit):
        parser().parse_args(['create', '--kvm', '--no-kvm'])


def test_information_reports_the_platform_in_use():
    record = {'id': 'sw-1', 'state': 'ready', 'spec': definition(kvm='auto')['spec']}
    assert summarize({**record, 'runtime_status': {'status': 'running', 'platform': 'kvm'}})['kvm'] is True
    assert summarize({**record, 'runtime_status': {'status': 'running', 'platform': 'systrap'}})['kvm'] is False
    assert 'kvm' not in summarize(record)


@pytest.mark.parametrize('kvm_request', [True, 'auto'])
def test_kvm_launches_need_an_engine_that_keeps_application_gs(monkeypatch, kvm_request):
    from sandweave.sandbox.runtimes.gvisor import driver, engine
    calls = []
    def feature_runtime(root, features, snapshot=None):
        calls.append(list(features))
        if 'kvm-gs' in features and snapshot == 'pinned-old':
            raise UnsupportedFeature('the captured runtime does not support: kvm-gs')
        return ['--runtime-build', 'new'] if 'kvm-gs' in features else []
    monkeypatch.setattr(engine, 'feature_runtime', feature_runtime)
    options = ['--platform', 'kvm']
    assert driver.select_runtime('root', [], None, kvm_request, options) == ['--runtime-build', 'new']
    assert options == ['--platform', 'kvm'] and calls == [['kvm-gs']]
    assert driver.select_runtime('root', ['disk-storage'], None, None, ['--platform', 'systrap']) == []
    options = ['--platform', 'kvm']
    if kvm_request == 'auto':
        # A pinned memory snapshot keeps its older engine and runs without KVM.
        assert driver.select_runtime('root', [], 'pinned-old', 'auto', options) == []
        assert options == ['--platform', 'systrap']
    else:
        with pytest.raises(UnsupportedFeature, match='kvm=False'):
            driver.select_runtime('root', [], 'pinned-old', True, options)


def test_engines_advertise_and_require_application_gs_on_kvm(tmp_path):
    from sandweave.sandbox.runtimes.gvisor.engine import supports
    import sandweave.sandbox.runtimes.gvisor.engine as engine
    build = tmp_path / 'tools/runtime-builds/x'
    build.mkdir(parents=True)
    descriptor = {'path': 'tools/runtime-builds/x', 'sha256': {'capabilities.json': 'h'}}
    original = engine.verify_runtime
    engine.verify_runtime = lambda *a: None
    try:
        (build / 'capabilities.json').write_text(json.dumps({'guest_clock': 1}))
        assert not supports(tmp_path, descriptor, ['kvm-gs'])
        (build / 'capabilities.json').write_text(json.dumps({'guest_clock': 1, 'kvm_gs': 1}))
        assert supports(tmp_path, descriptor, ['kvm-gs'])
    finally:
        engine.verify_runtime = original

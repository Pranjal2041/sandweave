import fcntl
from pathlib import Path


def test_group_admission_reuses_inventory_status_without_duplicate_runtime_reads():
    from sandweave.sandbox.admission import live
    parent = {'id': 'main', 'state': 'ready', 'runtime_status': {'status': 'stopped'},
              'services': {'sidecar': {'identity': 'sidecar'}}}
    child = {'id': 'sidecar', 'runtime_status': {'status': 'running'}}
    records = {'main': parent, 'sidecar': child}
    assert live(None, parent, records=records)
    child['runtime_status']['status'] = 'stopped'
    assert not live(None, parent, records=records)


def test_state_reader_uses_runsc_lock_before_parsing(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / 'scripts'))
    import environment
    state = tmp_path/'example_sandbox:example.state'
    assert environment.runtime_state(state) is None
    with state.with_suffix('.lock').open('w') as writer:
        fcntl.flock(writer, fcntl.LOCK_EX)
        state.write_text('')
        assert environment.runtime_state(state) is None
        state.write_text('{"status":"running"}')
        assert environment.runtime_state(state) is None
        fcntl.flock(writer, fcntl.LOCK_UN)
        assert environment.runtime_state(state) == {'status': 'running'}


def test_stale_runtime_pid_does_not_block_unrelated_admission(tmp_path, monkeypatch):
    import os
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / 'scripts'))
    import environment
    manager = environment.EnvironmentManager.__new__(environment.EnvironmentManager)
    manager.lab = manager.local = tmp_path
    bundle = tmp_path / 'gvisor/bundles/old'
    bundle.mkdir(parents=True)
    monkeypatch.setattr(manager, '_launcher', lambda name: None)
    monkeypatch.setattr(manager, '_settings', lambda name: {'runtime': 'test'})
    monkeypatch.setattr(environment, 'runtime_state', lambda path: {'status': 'running', 'sandbox': {'pid': os.getpid()}})
    monkeypatch.setattr(environment.runtime_store, 'validate', lambda *a, **k: tmp_path)
    status = manager.status('old')
    assert status['status'] == 'stopped'
    assert status['stale_runtime_pid'] == os.getpid()
    assert 'sentry' not in status
    monkeypatch.setattr(manager, '_launcher', lambda name: {'pid': 12, 'start': 34})
    assert manager.status('old')['status'] == 'starting'

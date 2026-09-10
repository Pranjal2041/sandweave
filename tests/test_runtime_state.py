import fcntl
from pathlib import Path


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

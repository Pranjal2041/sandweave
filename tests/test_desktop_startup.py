"""Desktop presentation changes share the configured readiness deadline."""
from types import SimpleNamespace

import pytest

from sandweave.templates.gnome import controls


def test_slow_overview_response_uses_remaining_readiness_budget(monkeypatch):
    clock = [10.0]
    calls = []

    def run(*, argv, timeout, **kwargs):
        calls.append(argv)
        if argv[0] == 'journalctl':
            return {'returncode': 0, 'stdout': b'Startup complete'}
        if 'org.freedesktop.DBus.Properties.Set' in argv:
            assert timeout == 50
            clock[0] += 8  # Longer than the former hidden five-second limit.
            return {'returncode': 0, 'stdout': b'()'}
        assert timeout == 42
        return {'returncode': 0, 'stdout': b'(<false>,)'}

    monkeypatch.setattr(controls.time, 'monotonic', lambda: clock[0])
    context = SimpleNamespace(run=run, remaining=lambda value: value)
    controls.AttachedDesktop._finish_startup(None, context, {}, 60)
    assert len(calls) == 3


def test_expired_readiness_does_not_start_overview_change(monkeypatch):
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        return {'returncode': 0, 'stdout': b'Startup complete'}

    monkeypatch.setattr(controls.time, 'monotonic', lambda: 60)
    context = SimpleNamespace(run=run, remaining=lambda value: value)
    with pytest.raises(TimeoutError, match='readiness deadline'):
        controls.AttachedDesktop._finish_startup(None, context, {}, 60)
    assert len(calls) == 1

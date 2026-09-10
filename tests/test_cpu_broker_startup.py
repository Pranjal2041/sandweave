import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest


def broker_module():
    spec = importlib.util.spec_from_file_location('cpu_broker_startup',
        Path(__file__).parents[1] / 'scripts/cpu_broker.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_registration_waits_for_its_own_acknowledgment(tmp_path, monkeypatch):
    broker = broker_module()
    monkeypatch.setattr(broker.subprocess, 'Popen', lambda *a, **kw: SimpleNamespace(poll=lambda: None))
    sleeps = []
    def acknowledge(seconds):
        sleeps.append(seconds)
        registration = next(tmp_path.glob('gvisor/cpu-brokers/*/job-test.json'))
        jobs = {} if len(sleeps) == 1 else {registration.name: {}}
        registration.with_name('status.json').write_text(json.dumps({'time': time.time(), 'jobs': jobs}))
    monkeypatch.setattr(broker.time, 'sleep', acknowledge)
    result = broker.register(tmp_path, 'test', {0}, 100, None)
    assert result.exists() and len(sleeps) == 2


def test_failed_controller_removes_unacknowledged_registration(tmp_path, monkeypatch):
    broker = broker_module()
    monkeypatch.setattr(broker.subprocess, 'Popen', lambda *a, **kw: SimpleNamespace(poll=lambda: 1))
    with pytest.raises(RuntimeError, match='acknowledge'):
        broker.register(tmp_path, 'test', {0}, 100, None)
    assert not list(tmp_path.glob('gvisor/cpu-brokers/*/job-*.json'))

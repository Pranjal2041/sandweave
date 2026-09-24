"""Guest service activation shares the caller's startup deadline."""
import subprocess
from types import SimpleNamespace

import pytest

from sandweave.sandbox.runtimes.gvisor import driver


def runtime(monkeypatch, run):
    instance = object.__new__(driver.Runtime)
    instance.connections = {}
    instance.manager = SimpleNamespace(_command=lambda identity: ['runsc'], _run=run,
                                       status=lambda identity: {'ports': {'23799': 12345}})
    monkeypatch.setattr(driver, 'Connection', lambda *a, **kw: SimpleNamespace(
        call=lambda *a: None, close=lambda: None))
    return instance


def test_slow_service_activation_uses_remaining_startup_budget(monkeypatch):
    clock = [0]
    def run(command, *, timeout):
        if 'systemd-run' in command:
            # Accept a slow bus response while staying inside the public 300s budget.
            assert timeout == 295
            assert '--no-block' in command  # readiness is checked through the agent
            clock[0] += 45
        else:
            assert timeout == 10
            clock[0] += 5
    instance = runtime(monkeypatch, run)
    monkeypatch.setattr(driver.time, 'monotonic', lambda: clock[0])
    result = instance._start_agent('env', 'token', 'systemd', True, 300,
                                   lambda: 300 - clock[0], ['python3', 'agent.py'])
    assert result['port'] == 12345
    assert clock[0] == 50


def test_control_bus_probe_cannot_outlive_startup_deadline(monkeypatch):
    clock = [0]
    def run(command, *, timeout):
        assert 'systemd-run' not in command
        assert timeout == 2
        clock[0] += timeout
        raise subprocess.TimeoutExpired(command, timeout)
    instance = runtime(monkeypatch, run)
    monkeypatch.setattr(driver.time, 'monotonic', lambda: clock[0])
    with pytest.raises(TimeoutError, match='control bus did not become ready'):
        instance._start_agent('env', 'token', 'systemd', True, 2,
                              lambda: 2 - clock[0], ['python3', 'agent.py'])

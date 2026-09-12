"""An interrupted client must reconnect to the worker it already started."""
import json
import os
from types import SimpleNamespace

import pytest

from sandweave.sandbox import targets, preparation


def test_interrupt_and_retry_reuses_preparing_worker(tmp_path, monkeypatch):
    installation = preparation.Installation(tmp_path, tmp_path/'assets')
    monkeypatch.setattr(preparation, 'ensure', lambda recipe: installation)
    monkeypatch.setattr(targets, 'worker_key', lambda assets: 'worker')
    metadata = tmp_path/'connections/worker/worker.json'
    starts = []
    def start(*args, **kwargs):
        starts.append(args)
        return SimpleNamespace(pid=os.getpid(), poll=lambda: None)
    monkeypatch.setattr(targets.subprocess, 'Popen', start)
    def interrupted(seconds):
        raise KeyboardInterrupt()
    monkeypatch.setattr(targets.time, 'sleep', interrupted)
    with pytest.raises(KeyboardInterrupt):
        targets.local_connection(template={})
    assert len(starts) == 1
    assert metadata.with_name('launcher.json').is_file()
    def ready(seconds):
        metadata.write_text(json.dumps({'port': 1234, 'token': 'test', 'pid': os.getpid()}))
    monkeypatch.setattr(targets.time, 'sleep', ready)
    monkeypatch.setattr(targets, 'Connection', lambda *a, **kw: SimpleNamespace(call=lambda *a: {}))
    targets.local_connection(template={})
    assert len(starts) == 1


def test_exiting_previous_worker_does_not_fail_the_next_creation(tmp_path, monkeypatch):
    from sandweave.sandbox import ownership
    installation = preparation.Installation(tmp_path, tmp_path/'assets')
    monkeypatch.setattr(preparation, 'ensure', lambda recipe: installation)
    monkeypatch.setattr(targets, 'worker_key', lambda assets: 'worker')
    directory = tmp_path / 'connections/worker'
    directory.mkdir(parents=True)
    metadata = directory / 'worker.json'
    (directory / 'launcher.json').write_text(json.dumps({'pid': -1}))
    old_checks = []
    def alive(record):
        if record['pid'] != -1:
            return True
        old_checks.append(True)
        return len(old_checks) <= 2
    starts = []
    def start(*args, **kwargs):
        starts.append(args)
        metadata.write_text(json.dumps({'port': 1234, 'token': 'test', 'pid': os.getpid()}))
        return SimpleNamespace(pid=os.getpid(), poll=lambda: None)
    monkeypatch.setattr(ownership, 'process_alive', alive)
    monkeypatch.setattr(targets.subprocess, 'Popen', start)
    monkeypatch.setattr(targets, 'Connection', lambda *a, **kw: SimpleNamespace(call=lambda *a: {}))
    targets.local_connection(template={})
    assert len(starts) == 1

"""Filesystem coverage follows backing storage, not the number of mount views."""
from pathlib import Path
import json
import subprocess
import sys
import time
import threading
from types import SimpleNamespace

import pytest


@pytest.fixture
def fs(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / 'scripts'))
    import filesystem_snapshot
    return filesystem_snapshot


ROOT = '1 0 0:1 / / rw - overlay none rw'
SPEC = {'mounts': [{'destination': '/data', 'type': 'tmpfs', 'options': ['mode=0750']}]}
DATA = '2 1 0:2 / /data rw - tmpfs none rw'


@pytest.mark.parametrize('extra', [
    '3 1 0:1 /workspace /workspace rw shared:4 - overlay none rw',
    '3 2 0:2 / /data rw shared:4 - tmpfs none rw',
    '3 2 0:2 /nested /data/nested rw - tmpfs none rw',
    '3 2 0:2 / /data rw - tmpfs none rw\n4 3 0:2 / /data rw - tmpfs none rw',
    '3 2 0:2 /nested /data/nested rw - tmpfs none rw\n4 3 0:2 /nested/child /data/nested/child rw - tmpfs none rw',
    '3 1 0:1 /with\\040space /with\\040space rw - overlay none rw',
])
def test_self_binds_do_not_duplicate_backing_filesystem(fs, extra):
    # Mountinfo order is not a traversal guarantee.
    records = [*extra.splitlines(), DATA, ROOT]
    assert fs.inventory(SPEC, '\n'.join(records)) == [
        {'destination': '/data', 'options': ['mode=0750'], 'file': 'mount-0.tar'}]


@pytest.mark.parametrize('extra', [
    '3 1 0:1 /elsewhere /workspace rw - overlay none rw',
    '3 1 0:9 /workspace /workspace rw - overlay none rw',
    '3 2 0:2 /elsewhere /data/nested rw - tmpfs none rw',
    '3 2 0:9 / /data rw - tmpfs none rw',
    '3 99 0:2 / /data rw - tmpfs none rw',
    '3 1 0:1 /elsewhere /var/lib/docker/alias rw - overlay none rw',
])
def test_distinct_or_unaccounted_storage_is_not_a_self_bind(fs, extra):
    with pytest.raises(ValueError):
        fs.inventory(SPEC, '\n'.join([ROOT, DATA, extra]))


def test_missing_persistent_mount_is_still_rejected(fs):
    with pytest.raises(ValueError, match='differs'):
        fs.inventory(SPEC, ROOT + '\n3 1 0:1 /data /data rw - overlay none rw')


def test_two_distinct_filesystems_at_same_path_remain_rejected(fs):
    with pytest.raises(ValueError, match='stacked'):
        fs.inventory(SPEC, '\n'.join([ROOT, DATA, '3 2 0:3 / /data rw - tmpfs none rw']))


def test_export_can_outlast_timeout_while_making_progress(fs, tmp_path):
    output = tmp_path / 'archive'
    script = 'import sys,time; f=open(sys.argv[1],"wb",buffering=0); [(f.write(b"data"),time.sleep(.05)) for _ in range(24)]'
    start = time.monotonic()
    with (tmp_path / 'log').open('wb') as log:
        fs.export([sys.executable, '-c', script, str(output)], output, log, .4)
    assert time.monotonic() - start > 1
    assert output.read_bytes() == b'data' * 24


def test_stalled_export_is_reaped(fs, tmp_path):
    output = tmp_path / 'archive'
    script = 'import sys,time; open(sys.argv[1],"w").write("started"); time.sleep(3); open(sys.argv[1],"w").write("late")'
    with (tmp_path / 'log').open('wb') as log, pytest.raises(TimeoutError, match='no progress'):
        fs.export([sys.executable, '-c', script, str(output)], output, log, .2)
    assert output.read_text() == 'started'


def test_export_failure_is_not_success(fs, tmp_path):
    with (tmp_path / 'log').open('wb') as log, pytest.raises(subprocess.CalledProcessError):
        fs.export([sys.executable, '-c', 'raise SystemExit(3)'], tmp_path / 'archive', log, 5)


@pytest.mark.parametrize('value', ['0', '-1', 'nan', 'inf', 'bad'])
def test_invalid_stall_timeout_rejected_before_pause(fs, monkeypatch, value):
    monkeypatch.setenv('SANDWEAVE_SNAPSHOT_STALL_TIMEOUT', value)
    with pytest.raises(ValueError):
        fs.capture([], 'unused', None, None, None, None)


def test_stall_timeout_can_be_configured(fs, monkeypatch):
    monkeypatch.setenv('SANDWEAVE_SNAPSHOT_STALL_TIMEOUT', '1800')
    assert fs.stall_timeout() == 1800


@pytest.mark.parametrize('status', ['running', 'paused'])
def test_export_error_preserves_original_pause_state(fs, monkeypatch, tmp_path, status):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, 'restore-mount', '')
    monkeypatch.setattr(fs.subprocess, 'run', run)
    monkeypatch.setattr(fs.subprocess, 'check_output', lambda command, **kw:
                        json.dumps({'status': status}) if 'state' in command else ROOT)
    def fail(*args):
        raise TimeoutError('stalled')
    monkeypatch.setattr(fs, 'export', fail)
    with pytest.raises(TimeoutError, match='stalled'):
        fs.capture(['runsc'], 'test', tmp_path, {'mounts': []}, tmp_path, tmp_path)
    assert [c[1] for c in calls] == (['tar', 'pause', 'resume'] if status == 'running' else ['tar'])


@pytest.mark.parametrize('budget', [1800, 1000])
def test_restore_uses_one_budget_beyond_old_stage_and_mount_limits(fs, monkeypatch, tmp_path, budget):
    # A virtual clock exercises minutes of healthy work without a minutes-long
    # test. Control processes still consume the exact waits passed by production.
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(fs, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    calls, children = [], []
    def probe(command, **kwargs):
        clock.now += kwargs['timeout']
        calls.append(command)
        if clock.now < 350:
            raise subprocess.TimeoutExpired(command, kwargs['timeout'])
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(fs.subprocess, 'run', probe)
    class Process:
        def __init__(self, command, **kwargs):
            self.command, self.returncode = command, None
            self.left = 650 if '--restore-mount' in command else .1
            self.killed = self.reaped = False
            children.append(self)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            assert self.reaped
        def communicate(self, timeout=None):
            if self.killed:
                self.reaped = True
                return b'', b''
            if self.left > timeout:
                self.left -= timeout
                clock.now += timeout
                raise subprocess.TimeoutExpired(self.command, timeout)
            clock.now += self.left
            self.left = 0
            self.returncode = 0
            self.reaped = True
            return b'', b''
        def kill(self):
            self.killed = True
    monkeypatch.setattr(fs.subprocess, 'Popen', Process)
    manifest = {'filesystem': {'mounts': [
        {'destination': '/data-' + str(i), 'options': [], 'file': f'mount-{i}.tar'} for i in range(2)]}}
    def restore():
        fs.finish_boot(['runtime'], 'test', tmp_path, manifest, tmp_path, tmp_path,
                       SimpleNamespace(poll=lambda: None), threading.Event(), deadline=budget)
    if budget == 1800:
        restore()
        assert 1650 <= clock.now < 1651
        assert len(children) == 3 and not any(p.killed for p in children)
        assert children[-1].command[-1] == '/run/engine-fs-ready'
    else:
        with pytest.raises(TimeoutError, match='startup_timeout.*unpacking /data-1'):
            restore()
        assert clock.now == budget
        assert len(children) == 1  # No second unpack or boot release after expiry.
    assert len(calls) == 35  # Boot staging itself exceeded the old 300 seconds.


def test_restore_probe_is_clipped_to_remaining_deadline(fs, monkeypatch, tmp_path):
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(fs, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    calls = []
    def probe(command, **kwargs):
        calls.append(kwargs['timeout'])
        clock.now += kwargs['timeout']
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])
    monkeypatch.setattr(fs.subprocess, 'run', probe)
    with pytest.raises(TimeoutError, match='startup_timeout.*boot staging'):
        fs.finish_boot(['runtime'], 'test', tmp_path, {'filesystem': {'mounts': []}}, tmp_path, tmp_path,
                       SimpleNamespace(poll=lambda: None), threading.Event(), deadline=.25)
    assert calls == [.25]


@pytest.mark.parametrize('failure', ['deadline', 'cancel', 'guest_exit'])
def test_restore_reaps_control_process_and_does_not_finish_after_abort(fs, tmp_path, failure):
    done = threading.Event()
    exited = threading.Event()
    pid_file = tmp_path / 'pid'
    marker = tmp_path / 'late'
    command = [sys.executable, '-c', 'import os,sys,time; '
        'open(sys.argv[1],"w").write(str(os.getpid())); time.sleep(20); '
        'open(sys.argv[2],"w").write("late")', str(pid_file), str(marker)]
    def abort():
        end = time.monotonic() + 3
        while not pid_file.exists() and time.monotonic() < end:
            time.sleep(.01)
        (done if failure == 'cancel' else exited).set()
    thread = threading.Thread(target=abort) if failure != 'deadline' else None
    if thread:
        thread.start()
    guest = SimpleNamespace(poll=lambda: 1 if exited.is_set() else None)
    error = TimeoutError if failure == 'deadline' else RuntimeError
    try:
        with pytest.raises(error, match='filesystem restore'):
            fs.restore_command(command, time.monotonic() + (1 if failure == 'deadline' else 10),
                               guest, done, 'unpacking /data')
    finally:
        if thread:
            thread.join()
    assert pid_file.exists()
    assert not Path('/proc', pid_file.read_text()).exists()
    assert not marker.exists()


def test_restore_reports_command_failure(fs):
    with pytest.raises(subprocess.CalledProcessError) as error:
        fs.restore_command([sys.executable, '-c', 'import sys; sys.stderr.write("invalid archive"); sys.exit(3)'],
                           time.monotonic() + 10, SimpleNamespace(poll=lambda: None), threading.Event(),
                           'unpacking /data')
    assert error.value.returncode == 3
    assert error.value.stderr == b'invalid archive'

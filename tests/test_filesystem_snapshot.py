"""Filesystem coverage follows backing storage, not the number of mount views."""
from pathlib import Path
import json
import subprocess
import sys
import time

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

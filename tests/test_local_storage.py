import json
from pathlib import Path

import pytest

from sandweave.sandbox import workspace
from test_onboarding import runtime_files


def test_tmpdir_and_explicit_directory_select_distinct_workers(runtime_files, tmp_path, monkeypatch):
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'home'))
    monkeypatch.delenv('SANDWEAVE_LOCAL_DIR', raising=False)
    monkeypatch.setenv('TMPDIR', str(tmp_path / 'temp'))
    first = workspace.prepare(source=runtime_files)
    local = Path(json.loads((first / 'prepared.json').read_text())['local'])
    assert local.parent == tmp_path / 'temp'
    assert local.stat().st_mode & 0o777 == 0o700
    assert workspace.prepare(source=runtime_files) == first
    monkeypatch.setenv('TMPDIR', str(tmp_path / 'other'))
    second = workspace.prepare(source=runtime_files)
    assert second != first
    assert Path(json.loads((second / 'prepared.json').read_text())['local']).parent == tmp_path / 'other'
    monkeypatch.setenv('SANDWEAVE_LOCAL_DIR', str(tmp_path / 'explicit'))
    third = workspace.prepare(source=runtime_files)
    assert third not in (first, second)
    assert Path(json.loads((third / 'prepared.json').read_text())['local']).parent == tmp_path / 'explicit'
    assert local.is_dir()  # Changing settings never relocates/deletes a live worker.


def test_explicit_directory_never_silently_falls_back(runtime_files, tmp_path, monkeypatch):
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'home'))
    file = tmp_path / 'not-a-directory'; file.write_text('keep')
    monkeypatch.setenv('SANDWEAVE_LOCAL_DIR', str(file))
    with pytest.raises(FileExistsError):
        workspace.prepare(source=runtime_files)
    assert file.read_text() == 'keep'

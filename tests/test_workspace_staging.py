import errno
import json
from pathlib import Path

import pytest

from sandweave import onboarding
from sandweave import installation
from sandweave.sandbox import workspace


@pytest.fixture
def isolated_default(tmp_path, monkeypatch):
    default = tmp_path / 'small-home'
    monkeypatch.delenv('SANDWEAVE_HOME', raising=False)
    monkeypatch.setattr(workspace, 'default_home', lambda: default)
    return default


def test_setup_keeps_data_with_selected_runtime(isolated_default, tmp_path):
    assets = tmp_path / 'large-disk'
    assets.mkdir()
    isolated_default.mkdir()
    original = {'targets': {'existing': {'job_id': '123'}}, 'onboarding_template': 'coding'}
    (isolated_default / 'config.json').write_text(json.dumps(original))
    storage = assets / '.sandweave'
    installation.publish(storage, assets, previous=original)
    assert workspace.home() == storage
    assert onboarding.configuration() == {**original, 'assets': str(assets)}
    assert json.loads((isolated_default / 'config.json').read_text()) == original
    assert json.loads((isolated_default / 'location.json').read_text()) == {'path': str(assets / '.sandweave')}
    assert not (isolated_default / 'workers').exists()
    # A later dependency selection keeps the already chosen data location.
    other = tmp_path / 'other-runtime'
    other.mkdir()
    onboarding.save_runtime_location(other)
    assert workspace.home() == assets / '.sandweave'
    assert onboarding.configuration()['assets'] == str(other)


def test_explicit_storage_is_preserved(isolated_default, tmp_path, monkeypatch):
    selected = tmp_path / 'custom-storage'
    monkeypatch.setenv('SANDWEAVE_HOME', str(selected))
    onboarding.save_runtime_location(tmp_path / 'assets')
    assert workspace.home() == selected
    assert not (isolated_default / 'location.json').exists()


def test_invalid_destination_config_does_not_publish_location(isolated_default, tmp_path):
    assets = tmp_path / 'assets'
    storage = assets / '.sandweave'
    storage.mkdir(parents=True)
    path = storage / 'config.json'
    path.write_text('unfinished user configuration')
    with pytest.raises(ValueError):
        installation.publish(storage, assets)
    assert not (isolated_default / 'location.json').exists()
    assert path.read_text() == 'unfinished user configuration'


def test_failed_storage_selection_cannot_fall_back_to_home(isolated_default, monkeypatch):
    from sandweave.cli import main
    monkeypatch.setattr(onboarding, 'inspect', lambda *a, **k: [onboarding.Check('assets', 'Runtime', 'pass', 'valid source')])

    def unavailable(*args, **kwargs):
        raise PermissionError('selected data directory is read-only')

    monkeypatch.setattr(onboarding, 'known_sources', lambda: [])
    monkeypatch.setattr(installation, 'destination', unavailable)
    monkeypatch.setattr(workspace, 'prepare', lambda: pytest.fail('staging continued in home'))
    assert main(['setup', '--yes', '--template', 'coding']) == 1
    assert not isolated_default.exists()


def cross_device(*args, **kwargs):
    raise OSError(errno.EXDEV, 'different filesystems')


@pytest.mark.parametrize('failure', [OSError(errno.ENOSPC, 'No space left on device'), KeyboardInterrupt()])
@pytest.mark.parametrize('previous', [None, b'old partial copy'])
def test_interrupted_copy_is_never_published(tmp_path, monkeypatch, failure, previous):
    source, target = tmp_path / 'source', tmp_path / 'target'
    contents = b'complete immutable image' * 100
    source.write_bytes(contents)
    if previous is not None:
        target.write_bytes(previous)
    monkeypatch.setattr(workspace.os, 'link', cross_device)
    real_copy = workspace.shutil.copy2

    def interrupt(src, dst):
        Path(dst).write_bytes(contents[:20])
        raise failure

    monkeypatch.setattr(workspace.shutil, 'copy2', interrupt)
    with pytest.raises((workspace.ResourceUnavailable, KeyboardInterrupt)):
        workspace._immutable(source, target)
    assert (target.read_bytes() if target.exists() else None) == previous
    assert not list(tmp_path.glob('.target.*'))
    monkeypatch.setattr(workspace.shutil, 'copy2', real_copy)
    workspace._immutable(source, target)
    assert target.read_bytes() == contents


def test_short_copy_without_error_is_rejected(tmp_path, monkeypatch):
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.write_bytes(b'complete image')
    monkeypatch.setattr(workspace.os, 'link', cross_device)
    monkeypatch.setattr(workspace.shutil, 'copy2', lambda src, dst: Path(dst).write_bytes(b'partial'))
    with pytest.raises(workspace.ResourceUnavailable, match='incomplete'):
        workspace._immutable(source, target)
    assert not target.exists()


def test_same_filesystem_staging_uses_hardlinks(tmp_path, monkeypatch):
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.write_bytes(b'complete image')
    monkeypatch.setattr(workspace.shutil, 'copy2', lambda *a: pytest.fail('unnecessary copy'))
    workspace._immutable(source, target)
    assert source.samefile(target)


def test_same_size_damaged_copy_is_replaced(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.write_bytes(b'complete image')
    target.write_bytes(b'x' * source.stat().st_size)
    workspace._immutable(source, target)
    assert target.read_bytes() == source.read_bytes()


def test_prepared_workspace_rechecks_truncated_image(tmp_path, monkeypatch):
    base = tmp_path / 'assets'
    for name in ('tools/bench', 'tools/seccomp-trap', 'tools/gs-base-probe', 'tools/debian-trixie.sif',
                 'images/gvisor-ubuntu-ready-ae303ca.erofs'):
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'complete runtime data')
    descriptor = base / 'tools/gvisor-socket/runtime.json'
    descriptor.parent.mkdir()
    descriptor.write_text('{}')
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'storage'))
    monkeypatch.setattr(workspace, 'assets', lambda: base)
    monkeypatch.setattr(workspace, 'engine_sources', lambda: scripts)
    monkeypatch.setattr(workspace, 'engine_files', lambda: [])
    mkdtemp = workspace.tempfile.mkdtemp
    monkeypatch.setattr(workspace.tempfile, 'mkdtemp', lambda **kw: mkdtemp(dir=tmp_path))
    prepared = workspace.prepare()
    local = json.loads((prepared / 'prepared.json').read_text())['local']
    image = prepared / 'images/gvisor-ubuntu-ready-ae303ca.erofs'
    image.unlink()  # Do not truncate the hardlinked source image.
    image.write_bytes(b'partial')
    assert workspace.prepare() == prepared
    assert image.read_bytes() == b'complete runtime data'
    assert json.loads((prepared / 'prepared.json').read_text())['local'] == local

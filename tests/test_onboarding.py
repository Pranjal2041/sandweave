import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sandweave import onboarding
from sandweave.cli import main, parser
from sandweave.templates.resolve import Template


@pytest.fixture
def runtime_files(tmp_path):
    root = tmp_path / 'assets'
    for name in ('tools/debian-trixie.sif', 'tools/bench', 'tools/seccomp-trap',
                 'tools/gs-base-probe', 'images/gvisor-ubuntu-ready-ae303ca.erofs'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'test runtime input')
    hashes = {}
    build = root / 'tools/runtime-builds/example'
    for name in ('runsc', 'gvisor-bin/gvisor_sentry', 'gvisor-bin/checkpointgofer',
                 'gvisor-bin/gvisor-sentry-prewarmer', 'gvisor-bin/runsc-metric-server'):
        path = build / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'test binary')
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (build / 'manifest.json').write_text(json.dumps(hashes))
    selected = root / 'tools/gvisor-socket/runtime.json'
    selected.parent.mkdir()
    selected.write_text(json.dumps({'path': 'tools/runtime-builds/example', 'sha256': hashes}))
    return root


def test_asset_validation_detects_corrupt_runtime(runtime_files):
    recipe = Template('coding').resolve()
    assert onboarding.validate_assets(runtime_files, recipe) == runtime_files
    (runtime_files / 'tools/runtime-builds/example/runsc').write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='checksum mismatch'):
        onboarding.validate_assets(runtime_files, recipe)


def test_asset_validation_rejects_manifest_escape(runtime_files, tmp_path):
    path = runtime_files / 'tools/gvisor-socket/runtime.json'
    config = json.loads(path.read_text())
    config['path'] = '../unrelated'
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match='escapes'):
        onboarding.validate_assets(runtime_files, Template('coding').resolve())


def test_save_preserves_targets_and_does_not_replace_invalid_config(tmp_path, monkeypatch):
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path))
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'targets': {'existing': {'job_id': '123'}}}))
    onboarding.save_configuration(assets='/prepared')
    assert json.loads(path.read_text()) == {'targets': {'existing': {'job_id': '123'}}, 'assets': '/prepared'}
    path.write_text('not json')
    with pytest.raises(ValueError):
        onboarding.save_configuration(assets='/other')
    assert path.read_text() == 'not json'


def test_assets_repair_persists_discovery_and_respects_decline(runtime_files, tmp_path, monkeypatch):
    home = tmp_path / 'home'
    monkeypatch.setenv('SANDWEAVE_HOME', str(home))
    monkeypatch.delenv('SANDWEAVE_ASSETS', raising=False)
    monkeypatch.setattr(onboarding.workspace, 'assets', lambda: runtime_files)
    monkeypatch.setattr(onboarding, 'asset_candidates', lambda: [])
    monkeypatch.setattr(onboarding, 'choose', lambda *a: 'cancel')
    assert onboarding.repair('assets', 'coding') is False
    assert not (home / 'config.json').exists()
    assert onboarding.repair('assets', 'coding', yes=True)
    assert onboarding.configuration()['assets'] == str(runtime_files)


def test_declined_package_install_does_not_run_pip(monkeypatch):
    monkeypatch.setattr(onboarding.importlib.util, 'find_spec', lambda name: None)
    monkeypatch.setattr(onboarding, 'confirm', lambda *args: False)
    monkeypatch.setattr(onboarding, 'install_packages', lambda *args: pytest.fail('install was declined'))
    assert not onboarding.repair('python', 'gnome')


def test_confirm_cancel_is_not_approval(monkeypatch):
    monkeypatch.setattr(onboarding, '_questionary', lambda: SimpleNamespace(
        confirm=lambda *args, **kwargs: SimpleNamespace(ask=lambda: None)))
    with pytest.raises(KeyboardInterrupt):
        onboarding.confirm('Install?')


def test_managed_tool_does_not_overwrite_existing_file(tmp_path, monkeypatch):
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path))
    original = tmp_path / 'bin/ffmpeg'
    original.parent.mkdir()
    original.write_text('user file')
    source = tmp_path / 'replacement'
    source.write_text('#!/bin/sh\nexit 0\n')
    source.chmod(0o755)
    with pytest.raises(ValueError, match='Refusing to replace'):
        onboarding.link_tool('ffmpeg', source)
    assert original.read_text() == 'user file'


def test_cli_setup_guest_compatibility_and_ambiguous_arguments():
    args = parser().parse_args(['setup', 'sandbox-id', 'install.sh', '--target', 'worker'])
    assert (args.id, args.script, args.target) == ('sandbox-id', 'install.sh', 'worker')
    assert main(['setup', 'sandbox-id']) == 1
    assert main(['setup', 'sandbox-id', 'install.sh', '--yes']) == 1
    assert main(['setup', '--target', 'worker']) == 1


@pytest.mark.parametrize('arguments', [['doctor', '--json'], ['doctor', '--check']])
def test_doctor_noninteractive_never_repairs_or_writes(arguments, monkeypatch, tmp_path, capsys):
    home = tmp_path / 'home'
    monkeypatch.setenv('SANDWEAVE_HOME', str(home))
    monkeypatch.setattr(onboarding, 'inspect', lambda *a, **k: [onboarding.Check('assets', 'Runtime', 'fail', 'Missing', 'Fix')])
    monkeypatch.setattr(onboarding, 'repair', lambda *a, **k: pytest.fail('read-only check repaired state'))
    assert main(arguments) == 1
    assert not home.exists()
    output = capsys.readouterr().out
    if '--json' in arguments:
        assert json.loads(output)['passed'] is False


def test_doctor_rechecks_after_selected_fix(monkeypatch, tmp_path):
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path))
    monkeypatch.setattr(onboarding.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(onboarding.sys.stdout, 'isatty', lambda: True)
    repaired = []
    inspections = []
    def inspect(*a, **k):
        inspections.append(True)
        return [onboarding.Check('assets', 'Runtime', 'pass' if repaired else 'fail', 'status', None if repaired else 'Fix')]
    actions = iter(['assets', 'exit'])
    monkeypatch.setattr(onboarding, 'inspect', inspect)
    monkeypatch.setattr(onboarding, 'choose', lambda *a: next(actions))
    monkeypatch.setattr(onboarding, 'repair', lambda name, *a: repaired.append(name))
    assert main(['doctor']) == 0
    assert repaired == ['assets'] and len(inspections) == 2


def test_setup_does_not_report_success_on_unresolved_host_block(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path))
    monkeypatch.setattr(onboarding, 'inspect', lambda *a, **k: [onboarding.Check('userns', 'Permissions', 'fail', 'Denied')])
    monkeypatch.setattr(onboarding, 'repair', lambda *a, **k: True)
    monkeypatch.setattr(onboarding, 'smoke_test', lambda: pytest.fail('cannot start blocked worker'))
    assert main(['setup', '--yes', '--template', 'coding']) == 1
    assert 'Setup complete.' not in capsys.readouterr().out


def test_setup_refuses_shadowed_asset_override(tmp_path, monkeypatch):
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path))
    monkeypatch.setenv('SANDWEAVE_ASSETS', '/selected-by-env')
    assert main(['setup', '--yes', '--assets', '/different']) == 1


def test_worker_identity_tracks_asset_selection_and_manifest(runtime_files, tmp_path, monkeypatch):
    from sandweave.sandbox.workspace import worker_key
    import shutil
    monkeypatch.setenv('SANDWEAVE_ASSETS', str(runtime_files))
    first = worker_key()
    other = tmp_path / 'other-assets'
    shutil.copytree(runtime_files, other)
    monkeypatch.setenv('SANDWEAVE_ASSETS', str(other))
    second = worker_key()
    assert first != second
    (other / 'sandweave-assets.json').write_text('{"schema_version": 1}')
    assert worker_key() != second


def test_desktop_requires_both_bridge_files(runtime_files):
    (runtime_files / 'tools/fast-io').mkdir()
    with pytest.raises(ValueError, match='bridge'):
        onboarding.validate_assets(runtime_files, Template('gnome').resolve())
    (runtime_files / 'tools/fast-io/bridge').write_bytes(b'bridge')
    with pytest.raises(ValueError, match='libxcb-xtest'):
        onboarding.validate_assets(runtime_files, Template('gnome').resolve())


def test_yes_never_opens_workload_menu_on_a_terminal(tmp_path, monkeypatch):
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path))
    monkeypatch.setattr(onboarding.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(onboarding.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(onboarding, 'choose', lambda *a: pytest.fail('--yes prompted'))
    monkeypatch.setattr(onboarding, 'inspect', lambda *a, **k: [onboarding.Check('userns', 'Permissions', 'fail', 'Denied')])
    monkeypatch.setattr(onboarding, 'repair', lambda *a, **k: True)
    assert main(['setup', '--yes']) == 1


def test_managed_ffmpeg_exports_distinct_paired_videos(tmp_path, monkeypatch):
    import io
    import shlex
    import shutil
    import subprocess
    pytest.importorskip('zstandard')
    pytest.importorskip('numpy')
    Image = pytest.importorskip('PIL.Image')
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('requires FFmpeg for actual encoding/decoding')
    from sandweave.templates.vr.frames import Frame, FrameRecorder, RecordedFrames
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'home'))
    binary = tmp_path / 'home/bin/ffmpeg'
    binary.parent.mkdir(parents=True)
    marker = tmp_path / 'used-managed-tool'
    binary.write_text('#!/bin/sh\nprintf "used\\n" >> ' + shlex.quote(str(marker)) +
                      '\nexec ' + shlex.quote(ffmpeg) + ' "$@"\n')
    binary.chmod(0o755)
    recording = FrameRecorder(tmp_path / 'frames')
    row = bytes([240, 10, 10, 255]) * 32 + bytes([10, 10, 240, 255]) * 32
    for i in range(3):
        recording.submit(Frame(i + 1, i + 1, 0, i * 100_000_000, 0, 0, 0, 0, 64, 32, row * 32, 2))
    recording.close()
    previews = [recording.video(), RecordedFrames(tmp_path / 'frames').video('left'),
                RecordedFrames(tmp_path / 'frames').video('right')]
    assert marker.read_text().count('used') == 3
    decoded = []
    for path in previews:
        png = subprocess.check_output([ffmpeg, '-v', 'error', '-i', str(path),
                                       '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-'])
        decoded.append(Image.open(io.BytesIO(png)).convert('RGB'))
    assert [image.size for image in decoded] == [(64, 32), (32, 32), (32, 32)]
    assert decoded[1].getpixel((16, 16))[0] > 200
    assert decoded[2].getpixel((16, 16))[2] > 200


def test_setup_declined_override_cannot_test_other_assets(tmp_path, monkeypatch):
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path))
    monkeypatch.delenv('SANDWEAVE_ASSETS', raising=False)
    monkeypatch.setattr(onboarding.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(onboarding.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(onboarding, 'inspect', lambda *a, **k: [onboarding.Check('assets', 'Runtime', 'pass', 'A')])
    monkeypatch.setattr(onboarding, 'confirm', lambda *a: False)
    monkeypatch.setattr(onboarding.workspace, 'prepare', lambda: pytest.fail('declined assets were used'))
    assert main(['setup', '--template', 'coding', '--assets', '/declined']) == 1
    assert not (tmp_path / 'config.json').exists()

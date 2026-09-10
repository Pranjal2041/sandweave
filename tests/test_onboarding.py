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


def test_assets_repair_uses_the_complete_setup_flow(monkeypatch):
    calls = []
    def setup(args, template, interactive):
        calls.append((args.assets, args.directory, args.yes, template, interactive))
        return 0 if args.yes else 1
    monkeypatch.setattr(onboarding, 'setup_worker', setup)
    assert not onboarding.repair('assets', 'coding')
    assert onboarding.repair('assets', 'gnome', yes=True, assets='/source')
    assert calls == [(None, None, False, 'coding', True), ('/source', None, True, 'gnome', False)]


@pytest.fixture
def setup_boundaries(monkeypatch, tmp_path):
    """Stub external setup work; each test controls the acceptance boundary."""
    monkeypatch.setattr(onboarding, 'known_sources', lambda: [])
    monkeypatch.setattr(onboarding.workspace, 'tool', lambda name: '/test/' + name)
    monkeypatch.setattr(onboarding, 'run_probe', lambda *a, **k: (True, 'available'))
    monkeypatch.setattr(onboarding, 'install_runtime', lambda *a, **k: tmp_path / 'installed')


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


def test_setup_does_not_report_success_on_unresolved_host_block(monkeypatch, tmp_path, capsys, setup_boundaries):
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path))
    monkeypatch.setattr(onboarding, 'inspect', lambda *a, **k: [onboarding.Check('userns', 'Permissions', 'fail', 'Denied')])
    monkeypatch.setattr(onboarding, 'repair', lambda *a, **k: True)
    monkeypatch.setattr(onboarding, 'smoke_test', lambda *a: pytest.fail('cannot start blocked worker'))
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


def test_yes_never_opens_workload_menu_on_a_terminal(tmp_path, monkeypatch, setup_boundaries):
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


def test_setup_declined_override_cannot_test_other_assets(tmp_path, monkeypatch, setup_boundaries):
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path))
    monkeypatch.delenv('SANDWEAVE_ASSETS', raising=False)
    monkeypatch.setattr(onboarding.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(onboarding.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(onboarding, 'inspect', lambda *a, **k: [onboarding.Check('assets', 'Runtime', 'pass', 'A')])
    monkeypatch.setattr(onboarding, 'confirm', lambda *a: False)
    monkeypatch.setattr(onboarding, 'ask_path', lambda *a, **k: tmp_path)
    monkeypatch.setattr(onboarding.workspace, 'prepare', lambda: pytest.fail('declined assets were used'))
    assert main(['setup', '--template', 'coding', '--assets', '/declined']) == 1
    assert not (tmp_path / 'config.json').exists()


def test_empty_destination_imports_source_without_publishing_config(runtime_files, tmp_path, monkeypatch):
    from sandweave.installation import import_runtime, validate_installation
    monkeypatch.setattr(onboarding.workspace, 'tool', lambda name: '/host/' + name)
    storage = tmp_path / 'new empty directory'
    installed = import_runtime(runtime_files, storage, Template('coding').resolve())
    assert installed.is_relative_to(storage / 'assets')
    assert installed != runtime_files
    assert not (storage / 'config.json').exists()
    validate_installation(installed)
    assert import_runtime(runtime_files, storage, Template('coding').resolve()) == installed


def test_corrupt_published_import_gets_separate_replacement(runtime_files, tmp_path, monkeypatch):
    from sandweave.installation import import_runtime, validate_installation
    monkeypatch.setattr(onboarding.workspace, 'tool', lambda name: '/host/' + name)
    storage = tmp_path / 'storage'
    recipe = Template('coding').resolve()
    first = import_runtime(runtime_files, storage, recipe)
    damaged = first / 'tools/bench'
    original = damaged.read_bytes()
    damaged.unlink()  # Leave its source hardlink unchanged.
    damaged.write_bytes(b'x' * len(original))
    second = import_runtime(runtime_files, storage, recipe)
    assert first != second and damaged.read_bytes() != original
    assert (second / 'tools/bench').read_bytes() == original
    validate_installation(second)


def test_same_size_image_corruption_is_rejected(runtime_files):
    image = 'images/gvisor-ubuntu-ready-ae303ca.erofs'
    path = runtime_files / image
    before = path.read_bytes()
    (runtime_files / 'sandweave-assets.json').write_text(json.dumps({'images': {
        image: {'size': len(before), 'sha256': hashlib.sha256(before).hexdigest()}}}))
    path.write_bytes(b'x' * len(before))
    with pytest.raises(ValueError, match='image checksum'):
        onboarding.validate_assets(runtime_files, Template('coding').resolve())


def test_new_image_can_supply_template_base_without_legacy_snapshot(runtime_files):
    image = 'images/gvisor-ubuntu-ready-ae303ca.erofs'
    path = runtime_files / image
    recipe = Template('coding').resolve()
    recipe['base_snapshot'] = 'example@1'
    registry = {'default_image': image, 'images': {image: {
        'size': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}},
        'snapshots': {'example@1': {'kind': 'image', 'path': image}}}
    (runtime_files / 'sandweave-assets.json').write_text(json.dumps(registry))
    assert onboarding.validate_assets(runtime_files, recipe) == runtime_files


def test_vr_validation_requires_graphics_helpers(runtime_files):
    # Isolate host-side graphics dependencies from the guest base image.
    recipe = Template('coding').resolve()
    recipe['installation'] = 'vr/opensaber'
    with pytest.raises(ValueError, match='vglrun'):
        onboarding.validate_assets(runtime_files, recipe)
    executable = runtime_files / 'tools/gpu/virtualgl/opt/VirtualGL/bin/vglrun'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'virtualgl wrapper')
    with pytest.raises(ValueError, match='libvisualorder'):
        onboarding.validate_assets(runtime_files, recipe)


def test_no_source_selects_bootstrap_with_destination_only(tmp_path, monkeypatch):
    from sandweave import bootstrap, releases
    calls = []
    class Builder:
        def __init__(self, directory):
            calls.append(directory)
        def build(self, template, recipe, *, base):
            calls.append((template, recipe['name'], base))
            return tmp_path / 'assets/built'
    monkeypatch.delenv('SANDWEAVE_ASSETS', raising=False)
    monkeypatch.setattr(bootstrap, 'Builder', Builder)
    monkeypatch.setattr(releases, 'check_host', lambda *a: {})
    monkeypatch.setattr(releases, 'install', lambda *a: None)
    assert onboarding.install_runtime('coding', tmp_path, sources=(), yes=True) == tmp_path / 'assets/built'
    assert calls == [tmp_path, ('coding', Template('coding').resolve()['name'], None)]


@pytest.mark.parametrize('parent', ['coding', 'gnome', 'docker', 'vr/gunspinning', 'games/gunspinning-gamepad'])
def test_custom_template_keeps_its_installation_dependency(tmp_path, parent):
    template = tmp_path / 'custom.toml'
    template.write_text('extends = ' + json.dumps(parent) + '\n')
    assert onboarding.workload(Template(template).resolve()) == parent


def test_copy_failure_never_falls_back_to_build(runtime_files, tmp_path, monkeypatch):
    from sandweave import installation, bootstrap
    monkeypatch.delenv('SANDWEAVE_ASSETS', raising=False)
    def fail(*args):
        raise OSError('quota exceeded')
    monkeypatch.setattr(installation, 'import_runtime', fail)
    monkeypatch.setattr(bootstrap, 'Builder', lambda *a: pytest.fail('unexpected rebuild'))
    with pytest.raises(OSError, match='quota exceeded'):
        onboarding.install_runtime('coding', tmp_path / 'storage', sources=[runtime_files], yes=True)


@pytest.mark.parametrize('failure', [RuntimeError('guest did not start'), KeyboardInterrupt()])
def test_setup_failure_preserves_saved_configuration_and_selection(tmp_path, monkeypatch, setup_boundaries, failure):
    monkeypatch.delenv('SANDWEAVE_HOME', raising=False)
    monkeypatch.delenv('SANDWEAVE_ASSETS', raising=False)
    default, selected = tmp_path / 'home', tmp_path / 'storage'
    default.mkdir(); selected.mkdir()
    monkeypatch.setattr(onboarding.workspace, 'default_home', lambda: default)
    before = {'assets': '/existing', 'targets': {'training': {'job_id': '123'}}}
    (default / 'config.json').write_text(json.dumps(before))
    destination_config = {'assets': '/previous-destination-assets'}
    (selected / 'config.json').write_text(json.dumps(destination_config))
    monkeypatch.setattr(onboarding, 'inspect', lambda *a, **k: [onboarding.Check('ok', 'Ready', 'pass', 'ready')])
    monkeypatch.setattr(onboarding.workspace, 'prepare', lambda: None)
    def smoke(template):
        assert onboarding.workspace.home() == selected
        assert onboarding.os.environ['SANDWEAVE_ASSETS'] == str(tmp_path / 'installed')
        raise failure
    monkeypatch.setattr(onboarding, 'smoke_test', smoke)
    args = SimpleNamespace(yes=True, directory=selected, assets=None, game_archive=None)
    with pytest.raises(type(failure)):
        onboarding.setup_worker(args, 'coding', False)
    assert json.loads((default / 'config.json').read_text()) == before
    assert json.loads((selected / 'config.json').read_text()) == destination_config
    assert not (default / 'location.json').exists()
    assert 'SANDWEAVE_HOME' not in onboarding.os.environ
    assert 'SANDWEAVE_ASSETS' not in onboarding.os.environ
    assert json.loads((selected / 'setup.json').read_text())['status'] == 'checking'


def test_setup_publishes_only_after_selected_workload_check(tmp_path, monkeypatch, setup_boundaries):
    monkeypatch.delenv('SANDWEAVE_HOME', raising=False)
    monkeypatch.delenv('SANDWEAVE_ASSETS', raising=False)
    default = tmp_path / 'home'
    monkeypatch.setattr(onboarding.workspace, 'default_home', lambda: default)
    selected = tmp_path / 'storage'
    monkeypatch.setattr(onboarding, 'inspect', lambda *a, **k: [onboarding.Check('ok', 'Ready', 'pass', 'ready')])
    monkeypatch.setattr(onboarding.workspace, 'prepare', lambda: None)
    checked = []
    def smoke(template):
        assert not (default / 'location.json').exists()
        assert not (selected / 'config.json').exists()
        checked.append(template)
    monkeypatch.setattr(onboarding, 'smoke_test', smoke)
    args = SimpleNamespace(yes=True, directory=selected, assets=None, game_archive=None)
    assert onboarding.setup_worker(args, 'coding', False) == 0
    assert checked == ['coding']
    assert onboarding.workspace.home() == selected
    assert onboarding.configuration()['assets'] == str(tmp_path / 'installed')
    assert json.loads((selected / 'setup.json').read_text())['status'] == 'ready'


@pytest.fixture
def first_use(tmp_path, monkeypatch, runtime_files):
    from sandweave.sandbox import preparation
    storage = tmp_path / 'chosen-storage'
    storage.mkdir()
    monkeypatch.setenv('SANDWEAVE_HOME', str(storage))
    monkeypatch.delenv('SANDWEAVE_ASSETS', raising=False)
    monkeypatch.setattr(onboarding.workspace, 'tool', lambda name: '/host/' + name)
    monkeypatch.setattr(onboarding.importlib.util, 'find_spec', lambda name: object())
    onboarding.workspace.atomic_json(storage / 'config.json', {'assets': str(runtime_files)})
    return preparation, storage, runtime_files


def desktop_files(root):
    for name in ('tools/fast-io/bridge', 'tools/fast-io/libxcb-xtest.so.0',
                 'tools/helpers/usr/include/X11/keysymdef.h',
                 'tools/helpers/usr/include/X11/XF86keysym.h'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'installed desktop input')


def test_first_use_adds_inherited_desktop_and_reuses_it(first_use, monkeypatch):
    preparation, storage, root = first_use
    recipe = Template({'extends': 'gnome', 'name': 'my-desktop'}).resolve()
    assert preparation.available(recipe, storage) is None
    installed = []
    def install(profile, selected):
        installed.append((profile, selected))
        desktop_files(root)
    monkeypatch.setattr(preparation, '_install', install)
    assert preparation.ensure(recipe) == preparation.Installation(storage, root)
    assert preparation.ensure(recipe).assets == root
    assert installed == [('gnome', storage)]
    assert onboarding.os.environ['SANDWEAVE_HOME'] == str(storage)
    assert 'SANDWEAVE_ASSETS' not in onboarding.os.environ


def test_first_use_without_setup_selects_current_directory(first_use, monkeypatch, tmp_path):
    from sandweave.installation import publish
    preparation, _, root = first_use
    monkeypatch.delenv('SANDWEAVE_HOME')
    small_home, project = tmp_path / 'small-home', tmp_path / 'project'
    project.mkdir()
    monkeypatch.setattr(onboarding.workspace, 'default_home', lambda: small_home)
    monkeypatch.chdir(project)
    installed = []
    def install(profile, selected):
        installed.append((profile, selected))
        publish(selected, root)
    monkeypatch.setattr(preparation, '_install', install)
    result = preparation.ensure(Template('coding').resolve())
    assert result.directory == project / '.sandweave'
    assert installed == [('coding', project / '.sandweave')]
    assert not (small_home / 'workers').exists()
    other = tmp_path / 'different-working-directory'
    other.mkdir()
    monkeypatch.chdir(other)
    assert preparation.ensure(Template('coding').resolve()) == result
    assert len(installed) == 1


def test_first_use_repairs_missing_python_dependency(first_use, monkeypatch):
    preparation, storage, root = first_use
    desktop_files(root)
    packages = set()
    monkeypatch.setattr(onboarding.importlib.util, 'find_spec',
                        lambda name: object() if name != 'PIL' or 'PIL' in packages else None)
    monkeypatch.setattr(preparation, '_install', lambda *a: packages.add('PIL'))
    assert preparation.ensure(Template('gnome').resolve()).assets == root
    assert packages == {'PIL'}


def test_failed_first_install_never_launches_a_worker(first_use, monkeypatch):
    from sandweave import Sandbox
    from sandweave.sandbox.errors import SetupError
    from sandweave.sandbox import targets
    preparation, _, _ = first_use
    def fail(*args):
        raise SetupError('download interrupted', phase='installation')
    monkeypatch.setattr(preparation, '_install', fail)
    monkeypatch.setattr(targets.subprocess, 'Popen', lambda *a, **k: pytest.fail('worker launched before dependencies'))
    with pytest.raises(SetupError, match='download interrupted'):
        Sandbox(template='gnome')


def test_reuse_checks_presence_without_reading_image_contents(first_use, monkeypatch):
    preparation, storage, root = first_use
    monkeypatch.setattr(onboarding.workspace, 'file_digest', lambda *a: pytest.fail('rehashing on reuse'))
    recipe = Template('coding').resolve()
    assert preparation.available(recipe, storage).assets == root
    (root / 'images/gvisor-ubuntu-ready-ae303ca.erofs').unlink()
    assert preparation.available(recipe, storage) is None


def test_automatic_source_extension_is_reused_by_later_connections(first_use, monkeypatch, tmp_path):
    import shutil
    from sandweave.installation import publish
    preparation, storage, original = first_use
    extended = tmp_path / 'extended'
    shutil.copytree(original, extended)
    desktop_files(extended)
    monkeypatch.setenv('SANDWEAVE_ASSETS', str(original))
    identity = onboarding.workspace.asset_identity(original)
    publish(storage, extended, source_identity=identity)
    monkeypatch.setattr(preparation, '_install', lambda *a: pytest.fail('source extension was ignored'))
    assert preparation.ensure(Template('gnome').resolve()).assets == extended
    assert onboarding.workspace.assets() == extended
    assert onboarding.os.environ['SANDWEAVE_ASSETS'] == str(original)
    # An explicit different source must not inherit the previous extension.
    alternate = tmp_path / 'alternate'
    shutil.copytree(original, alternate)
    monkeypatch.setenv('SANDWEAVE_ASSETS', str(alternate))
    assert onboarding.workspace.assets() == alternate
    assert preparation.available(Template('gnome').resolve(), storage) is None


def test_setup_sandbox_check_cannot_recursively_install(first_use, monkeypatch):
    preparation, storage, root = first_use
    monkeypatch.setattr(preparation, '_install', lambda *a: pytest.fail('recursive setup'))
    with preparation.checking(storage, root):
        assert preparation.ensure(Template('gnome').resolve()).assets == root
    assert preparation.available(Template('gnome').resolve(), storage) is None


def test_automatic_setup_leaves_launch_and_gpu_overrides_to_requested_sandbox(
        tmp_path, monkeypatch, setup_boundaries):
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path))
    monkeypatch.setattr(onboarding, 'inspect', lambda *a, **k: [
        onboarding.Check('assets', 'Runtime', 'pass', 'installed'),
        onboarding.Check('gpu', 'GPU', 'fail', 'No default GPU')])
    monkeypatch.setattr(onboarding, 'smoke_test', lambda *a: pytest.fail('automatic setup created a second sandbox'))
    monkeypatch.setattr(onboarding.workspace, 'prepare', lambda: pytest.fail('staged before actual constructor'))
    args = SimpleNamespace(yes=True, directory=tmp_path, assets=None, game_archive=None, _automatic=True)
    assert onboarding.setup_worker(args, 'coding', False) == 0
    assert json.loads((tmp_path / 'setup.json').read_text())['status'] == 'installed'


def test_waiting_installer_rechecks_completed_installation(first_use, monkeypatch):
    preparation, storage, _ = first_use
    monkeypatch.setattr(onboarding, '_setup_selected', lambda *a: pytest.fail('completed installation was repeated'))
    args = SimpleNamespace(yes=True, directory=storage, assets=None, game_archive=None, _automatic=True)
    assert onboarding.setup_worker(args, 'coding', False) == 0


def test_first_use_progress_keeps_stdout_available_for_command_results(first_use, monkeypatch, capsys):
    import io
    preparation, storage, _ = first_use
    commands = []
    class Installer:
        def __init__(self, command, **kwargs):
            commands.append(command)
            self.stdout = io.StringIO('Installing desktop files\nInstallation complete.\n')
        def wait(self):
            return 0
    monkeypatch.setattr(preparation.subprocess, 'Popen', Installer)
    preparation._install('gnome', storage)
    output = capsys.readouterr()
    assert output.out == ''
    assert 'Installing desktop files' in output.err
    assert commands[0][-4:] == ['--template', 'gnome', '--directory', str(storage)]
    logs = list((storage / 'logs/setup').glob('first-use-*.log'))
    assert len(logs) == 1 and logs[0].read_text() == output.err

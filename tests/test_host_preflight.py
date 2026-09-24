"""Unsupported workers must fail before setup, including cached installations."""
import json
from types import SimpleNamespace

import pytest

from sandweave import onboarding, releases
from sandweave.cli import main
from sandweave.sandbox import preparation
from sandweave.sandbox.errors import SetupError
from sandweave.templates.resolve import Template


@pytest.fixture
def unsupported_host(monkeypatch, tmp_path):
    monkeypatch.setattr(releases.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(releases.platform, 'machine', lambda: 'x86_64')
    monkeypatch.setattr(releases.platform, 'release', lambda: '5.3.0-46-generic')
    storage = tmp_path / 'uncreated-storage'
    monkeypatch.setenv('SANDWEAVE_HOME', str(storage))
    monkeypatch.delenv('SANDWEAVE_ASSETS', raising=False)
    def forbidden(*args, **kwargs):
        pytest.fail('unsupported host reached installation or runtime work')
    monkeypatch.setattr(onboarding, 'run_probe', forbidden)
    monkeypatch.setattr(onboarding, 'repair', forbidden)
    monkeypatch.setattr(onboarding, 'validate_assets', forbidden)
    monkeypatch.setattr(onboarding, 'ask_path', forbidden)
    return storage


@pytest.mark.parametrize('arguments', [['doctor', '--json'], ['doctor', '--check']])
def test_doctor_rejects_old_kernel_without_probes_or_writes(unsupported_host, arguments, capsys):
    assert main(arguments) == 1
    output = capsys.readouterr().out
    assert '5.3.0-46-generic' in output and 'Linux 5.4' in output
    if '--json' in arguments:
        report = json.loads(output)
        assert report['passed'] is False
        assert report['checks'][0]['status'] == 'fail'
        assert report['checks'][0]['fix'] is None
    assert not unsupported_host.exists()


@pytest.mark.parametrize('automatic', [False, True])
def test_setup_rejects_old_kernel_before_storage_or_apptainer(unsupported_host, automatic):
    args = SimpleNamespace(yes=automatic, directory=None, _automatic=automatic)
    with pytest.raises(ValueError, match='found 5.3.0-46-generic'):
        onboarding.setup_worker(args, 'gnome', interactive=not automatic)
    assert not unsupported_host.exists()


@pytest.mark.parametrize('prepared', [False, True])
def test_first_use_checks_kernel_even_with_prepared_files(unsupported_host, monkeypatch, prepared):
    monkeypatch.setattr(preparation, 'available', lambda *args: object() if prepared else None)
    monkeypatch.setattr(preparation, '_install', lambda *args: pytest.fail('started installer'))
    with pytest.raises(SetupError, match='Linux 5.4'):
        preparation.ensure(Template('gnome').resolve())
    assert not unsupported_host.exists()


@pytest.mark.parametrize('build', [False, True])
def test_runtime_import_or_build_cannot_bypass_kernel_check(unsupported_host, build):
    with pytest.raises(ValueError, match='Linux 5.4'):
        onboarding.install_runtime('coding', unsupported_host,
                                   sources=[unsupported_host / 'source'], build=build)
    assert not unsupported_host.exists()


@pytest.mark.parametrize('system,machine,release,compatible', [
    ('Linux', 'x86_64', '5.3.0-46-generic', False),
    ('Linux', 'x86_64', '5.4.0-216-generic', True),
    ('Linux', 'x86_64', '5.5.19', True),
    ('Linux', 'x86_64', '5.6.0', True),
    ('Linux', 'amd64', '5.15.0-126-generic', True),
    ('Linux', 'x86_64', '6.8.0-60-generic', True),
    ('Linux', 'x86_64', 'unknown', False),
    ('Linux', 'aarch64', '6.8.0', False),
    ('Darwin', 'x86_64', '24.0.0', False),
])
def test_shared_runtime_platform_requirements(monkeypatch, system, machine, release, compatible):
    monkeypatch.setattr(releases.platform, 'system', lambda: system)
    monkeypatch.setattr(releases.platform, 'machine', lambda: machine)
    monkeypatch.setattr(releases.platform, 'release', lambda: release)
    if compatible:
        assert releases.check_platform() == {
            'architecture': 'x86_64', 'kernel': releases.kernel_version(release)}
    else:
        with pytest.raises(ValueError):
            releases.check_platform()

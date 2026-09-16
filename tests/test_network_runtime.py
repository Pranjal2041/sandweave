import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sandweave import network_runtime
from sandweave.installation import needs_helpers
from test_onboarding import runtime_files


def test_host_passt_does_not_satisfy_missing_qualified_helper(runtime_files, monkeypatch):
    monkeypatch.setattr('sandweave.sandbox.workspace.tool', lambda name: '/host/' + name)
    assert not needs_helpers(runtime_files)
    (runtime_files / 'tools/network/passt').unlink()
    assert needs_helpers(runtime_files)


def test_old_network_revision_requires_upgrade(runtime_files):
    manifest = runtime_files / 'tools/network/manifest.json'
    manifest.write_text(json.dumps({'revision': 'older-build'}))
    assert not network_runtime.available(runtime_files)


def test_prebuilt_network_helper_preserves_existing_engine(runtime_files, tmp_path, monkeypatch):
    target = tmp_path / 'destination'
    engine = target / 'tools/gvisor-socket/runtime.json'
    engine.parent.mkdir(parents=True)
    engine.write_text('existing engine and snapshots')
    builder = SimpleNamespace(directory=tmp_path,
                              run=lambda *a, **k: pytest.fail('prebuilt helper was rebuilt'))
    from sandweave import releases
    monkeypatch.setattr(releases, 'install', lambda *a: runtime_files)
    network_runtime.install(builder, target)
    assert network_runtime.available(target)
    assert engine.read_text() == 'existing engine and snapshots'
    monkeypatch.setattr(releases, 'install', lambda *a: pytest.fail('installed helper downloaded again'))
    network_runtime.install(builder, target)


def test_network_launcher_ignores_host_passt(runtime_files, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / 'scripts'))
    import runtime_tools
    monkeypatch.setattr(runtime_tools.shutil, 'which', lambda name: '/host/' + name)
    command = runtime_tools.command(runtime_files, runtime_files / 'local', 'passt', '-f', memory_limit=True)
    assert command[0] == '/host/apptainer'
    assert '/host/passt' not in command
    assert str(runtime_files / 'tools/network/passt') in command
    assert '--as=536870912' in command
    (runtime_files / 'tools/network/passt').unlink()
    with pytest.raises(RuntimeError, match='passt is missing'):
        runtime_tools.command(runtime_files, runtime_files / 'local', 'passt')

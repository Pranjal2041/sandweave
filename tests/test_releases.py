import copy
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tarfile
import threading

import pytest

from sandweave import bootstrap, onboarding, releases
from sandweave.installation import record_installation


HOST = {'architecture': 'x86_64', 'kernel': (5, 14, 0), 'cpu_flags': ['sse2']}


@pytest.fixture
def published_runtime(tmp_path, monkeypatch):
    web = tmp_path / 'web'
    root = web / 'runtime'
    build = root / 'tools/runtime-builds/test-build'
    binaries = ('runsc', 'gvisor-bin/gvisor_sentry', 'gvisor-bin/checkpointgofer',
                'gvisor-bin/gvisor-sentry-prewarmer', 'gvisor-bin/runsc-metric-server')
    hashes = {}
    for name in binaries:
        path = build / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(('fixture binary: ' + name).encode())
        path.chmod(0o755)
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (build / 'manifest.json').write_text(json.dumps(hashes))
    pointer = root / 'tools/gvisor-socket/runtime.json'
    pointer.parent.mkdir()
    pointer.write_text(json.dumps({'path': 'tools/runtime-builds/test-build', 'sha256': hashes}))
    record_installation(root)
    archive = web / 'runtime.tar.gz'
    with tarfile.open(archive, 'w:gz') as stream:
        stream.add(root, arcname='runtime')
    requests = []
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(web), **kwargs)
        def log_message(self, *args):
            pass
        def do_GET(self):
            requests.append(self.path)
            super().do_GET()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    artifact = {'architecture': 'x86_64', 'minimum_kernel': '5.6', 'cpu_flags': ['sse2'],
                'name': archive.name, 'url': f'http://127.0.0.1:{server.server_port}/{archive.name}',
                'size': archive.stat().st_size, 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                'unpacked_bytes': sum(p.stat().st_size for p in root.rglob('*') if p.is_file())}
    manifest = {'schema_version': 1, 'version': 'fixture', 'artifacts': [artifact],
                'engine_patch_sha256': onboarding.workspace.file_digest(bootstrap.build_input('gvisor-no-kvm-prototype.patch'))}
    manifest_file = web / 'manifest.json'
    manifest_file.write_text(json.dumps(manifest))
    pin = tmp_path / 'pin.json'
    pin.write_text(json.dumps({'manifest': {'url': f'http://127.0.0.1:{server.server_port}/manifest.json',
        'sha256': hashlib.sha256(manifest_file.read_bytes()).hexdigest()}}))
    monkeypatch.setattr(releases, 'PIN', pin)
    monkeypatch.delenv('SANDWEAVE_ASSETS', raising=False)
    try:
        yield manifest, archive, requests, tmp_path / 'storage'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_release_download_verifies_complete_bundle_and_reuses_it(published_runtime):
    _, _, requests, storage = published_runtime
    installed = releases.install(storage, HOST)
    assert releases.validate_engine(installed)['path'] == 'tools/runtime-builds/test-build'
    assert releases.install(storage, HOST) == installed
    assert requests == ['/manifest.json', '/runtime.tar.gz']
    assert not list((storage / 'assets').glob('.release-*'))


def test_damaged_installed_release_is_preserved_and_replaced(published_runtime):
    _, _, requests, storage = published_runtime
    original = releases.install(storage, HOST)
    binary = original / 'tools/runtime-builds/test-build/runsc'
    binary.write_bytes(b'corrupted runtime')
    replacement = releases.install(storage, HOST)
    assert replacement != original
    assert binary.read_bytes() == b'corrupted runtime'
    releases.validate_engine(replacement)
    assert requests == ['/manifest.json', '/runtime.tar.gz']


@pytest.mark.parametrize('difference', ['architecture', 'kernel', 'cpu', 'engine'])
def test_incompatible_artifact_falls_back_without_downloading_it(published_runtime, monkeypatch, difference):
    manifest, _, requests, storage = published_runtime
    manifest = copy.deepcopy(manifest)
    artifact = manifest['artifacts'][0]
    if difference == 'architecture':
        artifact['architecture'] = 'aarch64'
    elif difference == 'kernel':
        artifact['minimum_kernel'] = '6.8'
    elif difference == 'cpu':
        artifact['cpu_flags'] = ['not-an-available-instruction']
    else:
        manifest['engine_patch_sha256'] = '0' * 64
    monkeypatch.setattr(releases, 'release_manifest', lambda *a: manifest)
    assert releases.install(storage, HOST) is None
    assert requests == [] and not storage.exists()


def test_corrupt_release_is_an_error_not_a_source_fallback(published_runtime):
    _, archive, _, storage = published_runtime
    archive.write_bytes(b'not the published archive')
    with pytest.raises(ValueError, match='checksum mismatch'):
        releases.install(storage, HOST)
    assert not list((storage / 'assets').iterdir())


def test_corrupt_manifest_is_an_error_before_binary_download(published_runtime):
    _, archive, requests, storage = published_runtime
    (archive.parent / 'manifest.json').write_text('{}')
    with pytest.raises(ValueError, match='checksum mismatch'):
        releases.install(storage, HOST)
    assert requests == ['/manifest.json']


def test_archive_size_limit_is_enforced_before_extracting_files(published_runtime, monkeypatch):
    manifest, _, _, storage = published_runtime
    manifest['artifacts'][0]['unpacked_bytes'] = 1
    monkeypatch.setattr(releases, 'release_manifest', lambda *a: manifest)
    with pytest.raises(ValueError, match='unpacked size'):
        releases.install(storage, HOST)
    assert not list((storage / 'assets').iterdir())


@pytest.mark.parametrize('archive_missing', [False, True])
def test_missing_release_allows_source_fallback(published_runtime, archive_missing):
    _, archive, _, storage = published_runtime
    (archive if archive_missing else archive.parent / 'manifest.json').unlink()
    assert releases.install(storage, HOST) is None


def test_setup_passes_downloaded_engine_to_template_builder(published_runtime, monkeypatch):
    _, _, requests, storage = published_runtime
    monkeypatch.setattr(releases, 'check_host', lambda *a: HOST)
    calls = []
    class Builder:
        def __init__(self, directory):
            assert directory == storage
        def build(self, profile, recipe, *, base, engine):
            releases.validate_engine(engine)
            calls.append((profile, base, engine))
            return storage / 'finished'
    monkeypatch.setattr(bootstrap, 'Builder', Builder)
    assert onboarding.install_runtime('coding', storage, yes=True) == storage / 'finished'
    assert calls[0][:2] == ('coding', None)
    assert requests == ['/manifest.json', '/runtime.tar.gz']


def test_blocked_host_never_downloads_or_builds(published_runtime, monkeypatch):
    _, _, requests, storage = published_runtime
    def blocked(*args):
        raise ValueError('host denied required namespaces')
    monkeypatch.setattr(releases, 'check_host', blocked)
    monkeypatch.setattr(bootstrap, 'Builder', lambda *a: pytest.fail('build cannot fix host policy'))
    with pytest.raises(ValueError, match='host denied'):
        onboarding.install_runtime('coding', storage, yes=True)
    assert requests == []


def test_explicit_build_skips_release_and_existing_installations(published_runtime, monkeypatch):
    _, _, requests, storage = published_runtime
    monkeypatch.setattr(releases, 'check_host', lambda *a: HOST)
    calls = []
    class Builder:
        def __init__(self, directory):
            pass
        def build(self, profile, recipe, *, base):
            calls.append(base)
            return storage / 'built'
    monkeypatch.setattr(bootstrap, 'Builder', Builder)
    assert onboarding.install_runtime('coding', storage, sources=[Path('/unused')], build=True) == storage / 'built'
    assert calls == [None] and requests == []


def test_namespace_failure_cleans_probe_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(releases, 'host_info', lambda: HOST)
    monkeypatch.setattr(onboarding.workspace, 'tool', lambda name: '/apptainer')
    monkeypatch.setattr(onboarding, 'run_probe', lambda *a, **k: (False, 'permission denied'))
    with pytest.raises(ValueError, match='rebuilding does not bypass host restrictions'):
        releases.check_host(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_host_probe_handles_symlinked_python_and_venv_under_tmp(tmp_path, monkeypatch):
    base = tmp_path / 'actual-python'
    (base / 'bin').mkdir(parents=True)
    (base / 'bin/python').touch()
    alias = tmp_path / 'python-alias'
    alias.symlink_to(base, target_is_directory=True)
    venv = tmp_path / 'venv'
    (venv / 'bin').mkdir(parents=True)
    (venv / 'bin/python').symlink_to(alias / 'bin/python')
    monkeypatch.setattr(releases, 'host_info', lambda: HOST)
    monkeypatch.setattr(releases.sys, 'base_prefix', str(alias))
    monkeypatch.setattr(releases.sys, 'executable', str(venv / 'bin/python'))
    monkeypatch.setattr(onboarding.workspace, 'tool', lambda name: '/apptainer')
    def probe(command, **kwargs):
        assert command[command.index('--no-mount') + 1] == 'tmp,home,cwd'
        assert str(base) + ':' + str(base) + ':ro' in command
        assert str(base / 'bin/python') in command
        assert not any(str(alias) in part or str(venv) in part for part in command)
        return True, 'NAMESPACE_AND_SECCOMP_OK'
    monkeypatch.setattr(onboarding, 'run_probe', probe)
    assert releases.check_host(tmp_path) == HOST


@pytest.mark.parametrize('architecture,kernel,message', [
    ('aarch64', '6.8.0', 'needs a port'), ('x86_64', '4.18.0', 'Linux 5.6')])
def test_source_requirements_are_not_binary_fallbacks(monkeypatch, architecture, kernel, message):
    monkeypatch.setattr(releases.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(releases.platform, 'machine', lambda: architecture)
    monkeypatch.setattr(releases.platform, 'release', lambda: kernel)
    with pytest.raises(ValueError, match=message):
        releases.host_info()

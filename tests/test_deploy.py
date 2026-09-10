"""Publication boundaries: committed input, immutable artifacts, retry and check mode."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest


@pytest.fixture
def deploy():
    spec = importlib.util.spec_from_file_location('deploy_tool', Path(__file__).parents[1] / 'scripts/deploy.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def files(tmp_path):
    paths = [tmp_path / 'sandweave-1.2.3-py3-none-any.whl', tmp_path / 'sandweave-1.2.3.tar.gz']
    for path in paths:
        path.write_bytes(path.name.encode())
    return paths


def test_upload_resumes_only_missing_matching_files(deploy, files):
    uploaded = {'urls': [{'filename': files[0].name, 'digests': {'sha256': deploy.digest(files[0])}}]}
    assert deploy.pending_files(files, None) == files
    assert deploy.pending_files(files, uploaded) == [files[1]]
    uploaded['urls'][0]['digests']['sha256'] = 'different'
    with pytest.raises(ValueError, match='different files'):
        deploy.pending_files(files, uploaded)


def test_validation_receipt_rejects_changed_artifacts(deploy, files):
    root = files[0].parent
    release = {'commit': 'source-commit', 'version': '1.2.3'}
    (root / 'validated.json').write_text(json.dumps({**release, 'files': {p.name: deploy.digest(p) for p in files}}))
    assert deploy.validated_files(root, release) == files
    with pytest.raises(ValueError, match='different release'):
        deploy.validated_files(root, {**release, 'commit': 'new-commit'})
    files[1].write_bytes(b'changed after tests')
    with pytest.raises(ValueError, match='artifact changed'):
        deploy.validated_files(root, release)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'repository with spaces'
    root.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()
    git('init', '-b', 'main')
    git('config', 'user.name', 'Release Test')
    git('config', 'user.email', 'release@example.invalid')
    git('remote', 'add', 'origin', 'https://github.com/example/sandweave.git')
    (root / 'pyproject.toml').write_text('[project]\nname="sandweave"\nversion="1.2.3"\n'
                                      '[project.urls]\nRepository="https://github.com/example/sandweave"\n')
    (root / 'CHANGELOG.md').write_text('# Changelog\n\n## 1.2.3\n\nA change.\n')
    git('add', 'pyproject.toml', 'CHANGELOG.md')
    git('commit', '-m', 'test release')
    return root, git


@pytest.mark.parametrize('staged', [False, True])
def test_uncommitted_changes_stop_before_publication(deploy, repo, staged):
    root, git = repo
    deploy.preflight(root)
    (root / 'CHANGELOG.md').write_text('uncommitted change')
    if staged:
        git('add', 'CHANGELOG.md')
    with pytest.raises(subprocess.CalledProcessError):
        deploy.preflight(root)


def test_untracked_user_files_are_not_archived(deploy, repo, tmp_path):
    root, git = repo
    (root / 'private.txt').write_text('user work')
    release = deploy.preflight(root)
    archive = tmp_path / 'committed.tar'
    git('archive', '--output=' + str(archive), release['commit'])
    with tarfile.open(archive) as contents:
        assert 'private.txt' not in contents.getnames()
    assert (root / 'private.txt').read_text() == 'user work'


def test_old_tag_and_wrong_origin_are_rejected(deploy, repo):
    root, git = repo
    git('tag', 'v1.2.3')
    git('commit', '--allow-empty', '-m', 'different commit')
    with pytest.raises(ValueError, match='another commit'):
        deploy.preflight(root)
    git('remote', 'set-url', 'origin', 'https://github.com/other/project.git')
    with pytest.raises(ValueError, match='does not match'):
        deploy.preflight(root)


def test_check_mode_does_not_push_or_publish(deploy, repo, monkeypatch):
    root, _ = repo
    monkeypatch.setattr(deploy, 'ROOT', root)
    monkeypatch.setattr(deploy, 'validate', lambda *args: ['validated-artifact'])
    monkeypatch.setattr(deploy, 'publish', lambda *args: pytest.fail('--check attempted publication'))
    monkeypatch.setattr(sys, 'argv', ['deploy', '--check'])
    deploy.main()


def test_pypi_conflict_stops_before_any_mutation(deploy, files, monkeypatch):
    release = {'name': 'sandweave', 'version': '1.2.3'}
    monkeypatch.setattr(deploy, 'read_json', lambda *a, **k: {
        'urls': [{'filename': files[0].name, 'digests': {'sha256': 'different'}}]})
    monkeypatch.setattr(deploy, 'run', lambda *a, **k: pytest.fail('conflict should stop before commands'))
    monkeypatch.setattr(deploy, 'token', lambda: pytest.fail('conflict should stop before requesting credentials'))
    with pytest.raises(ValueError, match='different files'):
        deploy.publish(files[0].parent, release, files)


def test_failed_recheck_invalidates_previous_pass(deploy, repo, monkeypatch):
    root, _ = repo
    release = deploy.preflight(root)
    directory = root / 'runs/deploy' / (release['version'] + '-' + release['commit'][:12])
    directory.mkdir(parents=True)
    receipt = directory / 'validated.json'
    receipt.write_text('{}')
    monkeypatch.setattr(deploy, 'ROOT', root)
    def failed(*args):
        raise ValueError('new check failed')
    monkeypatch.setattr(deploy, 'validate', failed)
    monkeypatch.setattr(sys, 'argv', ['deploy', '--check', '--recheck'])
    with pytest.raises(ValueError, match='new check failed'):
        deploy.main()
    assert not receipt.exists()


def test_credentials_are_parsed_as_data(deploy, tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, 'ROOT', tmp_path)
    for key in ('UV_PUBLISH_TOKEN', 'PYPI_API_KEY', 'PYPI_API_TOKEN'):
        monkeypatch.delenv(key, raising=False)
    value = 'pypi-example-$(touch should-not-exist)'
    (tmp_path / '.env').write_text(f'PYPI_API_KEY="{value}"\n')
    assert deploy.token() == value
    assert not (tmp_path / 'should-not-exist').exists()

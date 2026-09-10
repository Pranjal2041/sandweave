#!/usr/bin/env python3
"""Validate the committed SDK and publish it to GitHub Releases and PyPI."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def run(*args, cwd=ROOT, env=None, capture=False):
    print('+ ' + shlex.join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), cwd=cwd, env=env, check=True,
                          text=True, stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.STDOUT if capture else None).stdout


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_json(url, *, missing=False):
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if missing and error.code == 404:
            return None
        raise


def preflight(root):
    # git diff --quiet includes unstaged and staged changes against HEAD.
    run('git', 'diff', '--exit-code', '--quiet', 'HEAD', cwd=root)
    project = tomllib.loads((root / 'pyproject.toml').read_text())['project']
    version = project['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Use a stable X.Y.Z project.version for this release command')
    repository = project['urls']['Repository'].removeprefix('https://github.com/').removesuffix('.git')
    origin = run('git', 'remote', 'get-url', '--push', 'origin', cwd=root, capture=True).strip()
    if origin not in (f'https://github.com/{repository}', f'https://github.com/{repository}.git',
                      f'git@github.com:{repository}.git'):
        raise ValueError('origin does not match project.urls.Repository')
    commit = run('git', 'rev-parse', 'HEAD', cwd=root, capture=True).strip()
    branch = run('git', 'symbolic-ref', '--short', 'HEAD', cwd=root, capture=True).strip()
    tag = 'v' + version
    tags = run('git', 'tag', '--list', tag, cwd=root, capture=True).strip()
    if tags and run('git', 'rev-list', '-n', '1', tag, cwd=root, capture=True).strip() != commit:
        raise ValueError(f'{tag} already points to another commit; bump project.version')
    headings = (root / 'CHANGELOG.md').read_text().split('## ')
    notes = next((s.split('\n', 1)[1].strip() for s in headings if s.startswith(version + '\n')), None)
    if not notes:
        raise ValueError('Add this version to CHANGELOG.md before releasing')
    return dict(name=project['name'], version=version, repository=repository, commit=commit,
                branch=branch, tag=tag, notes=notes)


def wheel_contents(path):
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist() if not name.endswith('/')}


def validated_files(directory, release):
    receipt = directory / 'validated.json'
    if not receipt.exists():
        return None
    saved = json.loads(receipt.read_text())
    if saved['commit'] != release['commit'] or saved['version'] != release['version']:
        raise ValueError('Validation receipt belongs to a different release')
    files = [directory / name for name in saved['files']]
    if len(files) != 2 or not any(p.suffix == '.whl' for p in files) or not any(p.name.endswith('.tar.gz') for p in files):
        raise ValueError('Validation receipt must identify a wheel and source distribution')
    for path in files:
        if path.parent != directory or digest(path) != saved['files'][path.name]:
            raise ValueError('Validated artifact changed: ' + path.name)
    return files


def validate(directory, release):
    work = Path(tempfile.mkdtemp(prefix='sandweave-deploy-')).resolve()
    if work.is_relative_to(ROOT):
        raise ValueError('Set TMPDIR outside this checkout for installed-package acceptance')
    print('Validation workspace: ' + str(work), flush=True)
    source = work / 'source'
    source.mkdir()
    run('git', 'archive', '--format=tar', '--output=' + str(work / 'source.tar'), release['commit'])
    run('tar', '-xf', work / 'source.tar', '-C', source)
    env = {k: v for k, v in os.environ.items() if k not in (
        'PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'SANDWEAVE_HOME', 'SANDWEAVE_ASSETS',
        'SANDWEAVE_INTEGRATION', 'UV_PUBLISH_TOKEN', 'PYPI_API_KEY', 'PYPI_API_TOKEN')}
    env['SOURCE_DATE_EPOCH'] = run('git', 'show', '-s', '--format=%ct', release['commit'], capture=True).strip()
    dist = work / 'dist'
    run('uv', 'build', '--out-dir', dist, source, cwd=work, env=env)
    files = [*dist.glob('*.whl'), *dist.glob('*.tar.gz')]
    if len(files) != 2:
        raise ValueError('Build must produce exactly one wheel and one source distribution')
    wheel, sdist = files
    run('uvx', 'twine', 'check', '--strict', *files, cwd=work, env=env)
    unpacked = work / 'sdist'
    unpacked.mkdir()
    with tarfile.open(sdist) as archive:
        archive.extractall(unpacked, filter='data')
    run('uv', 'build', '--wheel', '--out-dir', work / 'rebuilt', next(unpacked.iterdir()), cwd=work, env=env)
    if wheel_contents(wheel) != wheel_contents(next((work / 'rebuilt').glob('*.whl'))):
        raise ValueError('Source distribution does not reproduce the wheel contents')
    python = work / 'venv/bin/python'
    run('uv', 'venv', '--python', sys.executable, python.parents[1], cwd=work, env=env)
    run('uv', 'pip', 'install', '--python', python, str(wheel) + '[test,vr]', cwd=work, env=env)
    run(python, '-m', 'pytest', '-q', '-m', 'not integration and not gpu',
        '--confcutdir=' + str(source), cwd=source, env=env)
    run(python, '-c', 'import sandweave; from importlib.metadata import version; '
        f'assert sandweave.__version__ == version("sandweave") == {release["version"]!r}', cwd=work, env=env)
    live = work / 'acceptance'
    live.mkdir()
    shutil.copyfile(source / 'tests/integration/test_sdk.py', live / 'test_sdk.py')
    (live / 'pytest.ini').write_text('[pytest]\nmarkers = integration: installed SDK acceptance\n')
    env.update(SANDWEAVE_HOME=str(work / 'worker'), SANDWEAVE_INTEGRATION='1')
    run(python, '-m', 'pytest', '-q', '-s', '--confcutdir=' + str(live), cwd=live, env=env)
    # Copy artifacts only after every check passes. A receipt is never partial.
    for path in files:
        shutil.copyfile(path, directory / path.name)
    receipt = {**release, 'workspace': str(work), 'files': {p.name: digest(p) for p in files}}
    temporary = directory / 'validated.json.tmp'
    temporary.write_text(json.dumps(receipt, indent=2) + '\n')
    temporary.replace(directory / 'validated.json')
    return [directory / p.name for p in files]


def pending_files(files, published):
    """Resume a partial upload only if every existing file has identical bytes."""
    expected = {p.name: digest(p) for p in files}
    for item in (published or {}).get('urls', []):
        if expected.get(item['filename']) != item['digests']['sha256']:
            raise ValueError('PyPI already has different files for this version: ' + item['filename'])
    present = {item['filename'] for item in (published or {}).get('urls', [])}
    return [p for p in files if p.name not in present]


def token():
    for key in ('UV_PUBLISH_TOKEN', 'PYPI_API_KEY', 'PYPI_API_TOKEN'):
        if os.environ.get(key):
            return os.environ[key]
    path = ROOT / '.env'
    if path.is_file():
        for line in path.read_text().splitlines():
            key, sep, value = line.partition('=')
            if sep and key.strip() in ('UV_PUBLISH_TOKEN', 'PYPI_API_KEY', 'PYPI_API_TOKEN'):
                value = value.strip().strip('"\'')
                if value:
                    return value
    if shutil.which('ut'):
        # ut may print the credential; capture it rather than logging it.
        result = run('ut', 'api-key', 'request', 'pypi', capture=True)
        match = re.search(r'pypi-[A-Za-z0-9_-]+', result)
        if match:
            return match.group()
    raise ValueError('Set UV_PUBLISH_TOKEN or PYPI_API_KEY for publication')


def publish(directory, release, files):
    url = f'https://pypi.org/pypi/{release["name"]}/{release["version"]}/json'
    pending = pending_files(files, read_json(url, missing=True))
    upload_token = token() if pending else None
    repo, tag = release['repository'], release['tag']
    run('gh', 'auth', 'status')
    # Query releases once; failures must not be mistaken for an absent release.
    pages = json.loads(run('gh', 'api', '--paginate', '--slurp',
        f'repos/{repo}/releases?per_page=100', capture=True))
    existing = next((r for page in pages for r in page if r['tag_name'] == tag), None)
    for asset in (existing or {}).get('assets', []):
        local = next((p for p in files if p.name == asset['name']), None)
        if local is None or asset.get('digest') != 'sha256:' + digest(local):
            raise ValueError('GitHub release contains different assets: ' + asset['name'])
    # Non-forced pushes reject remote tags/branches that changed independently.
    run('git', 'push', 'origin', f'{release["commit"]}:refs/heads/{release["branch"]}')
    if not run('git', 'tag', '--list', tag, capture=True).strip():
        run('git', 'tag', '-a', tag, release['commit'], '-m', f'Sandweave {release["version"]}')
    run('git', 'push', 'origin', 'refs/tags/' + tag)
    notes = directory / 'release-notes.md'
    notes.write_text(release['notes'] + '\n')
    if existing is None:
        run('gh', 'release', 'create', tag, *files, '--repo', repo, '--verify-tag', '--draft',
            '--title', 'Sandweave ' + release['version'], '--notes-file', notes)
    else:
        present = {a['name'] for a in existing['assets']}
        missing = [p for p in files if p.name not in present]
        if missing:
            run('gh', 'release', 'upload', tag, *missing, '--repo', repo)
    if pending:
        run('uv', 'publish', '--publish-url', 'https://upload.pypi.org/legacy/', *pending,
            env={**os.environ, 'UV_PUBLISH_TOKEN': upload_token})
    for attempt in range(12):
        if not pending_files(files, read_json(url, missing=True)):
            break
        print('Waiting for the PyPI version index...', flush=True)
        time.sleep(5)
    else:
        raise ValueError('PyPI has not exposed both matching files yet; rerun ./deploy')
    run('gh', 'release', 'edit', tag, '--repo', repo, '--draft=false', '--latest')
    published = json.loads(run('gh', 'api', f'repos/{repo}/releases/tags/{tag}', capture=True))
    expected = {p.name: 'sha256:' + digest(p) for p in files}
    actual = {a['name']: a.get('digest') for a in published['assets']}
    if published['draft'] or actual != expected:
        raise ValueError('GitHub release verification failed; rerun ./deploy')
    (directory / 'published.json').write_text(json.dumps({**release, 'files': {p.name: digest(p) for p in files}}, indent=2) + '\n')
    print(f'Published https://pypi.org/project/{release["name"]}/{release["version"]}/', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='validate without pushing or publishing')
    parser.add_argument('--recheck', action='store_true', help='repeat validation instead of reusing its receipt')
    args = parser.parse_args()
    if not shutil.which('uv') or (not args.check and not shutil.which('gh')):
        raise ValueError('Install uv and, for publication, the GitHub CLI')
    release = preflight(ROOT)
    directory = ROOT / 'runs/deploy' / (release['version'] + '-' + release['commit'][:12])
    directory.mkdir(parents=True, exist_ok=True)
    print('Release files and logs: ' + str(directory), flush=True)
    with (directory / 'deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Tee subprocess output too, keeping the terminal useful during setup.
        sys.stdout.flush()
        tee = subprocess.Popen(['tee', '-a', str(directory / 'deploy.log')], stdin=subprocess.PIPE)
        saved_stdout, saved_stderr = os.dup(1), os.dup(2)
        os.dup2(tee.stdin.fileno(), 1)
        os.dup2(tee.stdin.fileno(), 2)
        try:
            files = None if args.recheck else validated_files(directory, release)
            files = files or validate(directory, release)
            if preflight(ROOT) != release:
                raise ValueError('Source changed during validation; start a new release check')
            if args.check:
                print('Checks passed. Run ./deploy to publish these artifacts.', flush=True)
            else:
                publish(directory, release, files)
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(saved_stdout, 1)
            os.dup2(saved_stderr, 2)
            os.close(saved_stdout)
            os.close(saved_stderr)
            tee.stdin.close()
            tee.wait()


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit('deploy: ' + str(error))

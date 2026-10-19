"""Named OSWorld subsets retain membership, order, source bytes and patches."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace

import pytest

from sandweave import Benchmark
from sandweave.benchmarks import source


@pytest.fixture
def reference(tmp_path, monkeypatch):
    root = tmp_path / 'reference'
    root.mkdir()
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'state'))
    monkeypatch.setattr(source, 'cua', lambda path: root)
    # The pinned reference owns manifest validation. These fixtures exercise
    # Sandweave's selection, cache verification and materialization around it.
    monkeypatch.setattr(source, 'module', lambda path: SimpleNamespace(
        _read_spec=lambda spec: json.loads(spec.read_text())))
    bodies = {'chrome/first': {'instruction': 'First task'},
              'os/second': {'instruction': 'Second task'}}
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        if url.endswith('test_all.json'):
            return io.BytesIO(json.dumps({'chrome': ['first'], 'os': ['second']}).encode())
        key = url.split('/examples/', 1)[1].removesuffix('.json')
        return io.BytesIO(json.dumps(bodies[key]).encode())

    monkeypatch.setattr(source.urllib.request, 'urlopen', fetch)
    selected = []
    for key in ('os/second', 'chrome/first'):
        data = (json.dumps(bodies[key], indent=2) + '\n').encode()
        selected.append({'id': 'osworld_' + key.replace('/', '_'),
                         'source_sha256': hashlib.sha256(data).hexdigest()})
    for name in ('osworld-50', 'osworld-offline'):
        spec = root / 'benchmarks' / name / 'benchmark-source.yaml'
        spec.parent.mkdir(parents=True)
        spec.write_text(json.dumps({'tasks': selected}))
    patch = root / 'scripts/osworld_task_patches/os__second.json'
    patch.parent.mkdir(parents=True)
    patch.write_text('{"config": []}')
    return root, selected, calls


@pytest.mark.parametrize('name', ['osworld-energy50-representative', 'osworld-unanimous-295'])
def test_named_split_does_not_fall_back_to_full_manifest(reference, name):
    root, selected, calls = reference
    bench = Benchmark(name)
    try:
        assert [task.id for task in bench.tasks] == [item['id'] for item in selected]
        assert [task.instruction for task in bench.tasks] == ['Second task', 'First task']
        for task, item in zip(bench.tasks, selected):
            directory = Path(task.metadata['directory'])
            assert hashlib.sha256((directory / 'source.json').read_bytes()).hexdigest() == item['source_sha256']
        directory = Path(bench.tasks[0].metadata['directory'])
        assert (directory / 'setup-patch.json').read_bytes() == (
            root / 'scripts/osworld_task_patches/os__second.json').read_bytes()
        assert not any(url.endswith('test_all.json') for url in calls)
        calls.clear()
        assert source.tasks(root, name) == bench.tasks
        assert calls == []  # Existing source files need no network request.
        (directory / 'source.json').write_text('{"instruction": "corrupted"}')
        assert source.tasks(root, name) == bench.tasks
        assert len(calls) == 1  # Repair just the corrupted task.
    finally:
        bench.close()


def test_full_osworld_keeps_its_canonical_manifest(reference):
    _, _, calls = reference
    bench = Benchmark('osworld')
    try:
        assert [task.id for task in bench.tasks] == ['osworld_chrome_first', 'osworld_os_second']
        assert sum(url.endswith('test_all.json') for url in calls) == 1
    finally:
        bench.close()


def test_subset_rejects_different_source_bytes(reference):
    root, selected, _ = reference
    spec = root / 'benchmarks/osworld-offline/benchmark-source.yaml'
    selected[0]['source_sha256'] = '0' * 64
    spec.write_text(json.dumps({'tasks': selected}))
    with pytest.raises(ValueError, match='source checksum mismatch'):
        Benchmark('osworld-unanimous-295')


def test_legacy_subset_paths_and_separate_cache(reference, monkeypatch):
    root, _, _ = reference
    public_tasks = source.tasks(root, 'osworld-unanimous-295')
    for old, new in source.SUBSETS.items():
        (root / 'benchmarks' / new).rename(root / 'benchmarks' / old)
    monkeypatch.setattr(source, 'cua_revision', lambda path: source.LEGACY_CUA_REVISION)
    legacy_tasks = source.tasks(root, 'osworld-unanimous-295')
    assert [t.id for t in public_tasks] == [t.id for t in legacy_tasks]
    assert public_tasks[0].metadata['directory'] != legacy_tasks[0].metadata['directory']


def test_missing_public_patch_does_not_reuse_stale_patch(reference):
    root, _, _ = reference
    task = source.tasks(root, 'osworld-unanimous-295')[1]
    patch = Path(task.metadata['directory']) / 'setup-patch.json'
    patch.write_text('{"config": []}')
    source.tasks(root, 'osworld-unanimous-295')
    assert not patch.exists()


@pytest.mark.parametrize('valid', [True, False])
def test_public_download_without_github_cli_or_credentials(tmp_path, monkeypatch, valid):
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'state'))
    # Any subprocess (including gh or git) is forbidden on the download path.
    def forbidden(*args, **kwargs):
        pytest.fail('public source download invoked a subprocess')
    monkeypatch.setattr(source.subprocess, 'run', forbidden)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w:gz') as tar:
        data = b'public source'
        member = tarfile.TarInfo('upstream/scripts/probe.py')
        member.size = len(data)
        tar.addfile(member, io.BytesIO(data))
    data = archive.getvalue()
    monkeypatch.setattr(source, 'CUA_ARCHIVE_SHA256', hashlib.sha256(data).hexdigest() if valid else '0' * 64)
    calls = []
    def fetch(url, timeout):
        calls.append(url)
        return io.BytesIO(data)
    monkeypatch.setattr(source.urllib.request, 'urlopen', fetch)
    if valid:
        path = source.cua()
        assert (path / 'scripts/probe.py').read_bytes() == b'public source'
        assert source.cua() == path
    else:
        with pytest.raises(ValueError, match='archive checksum mismatch'):
            source.cua()
        assert not (tmp_path / 'state/benchmarks/sources' / ('cua-' + source.CUA_REVISION)).exists()
    assert calls == ['https://codeload.github.com/cua-speedrun/cua-speedrun/tar.gz/' + source.CUA_REVISION]


@pytest.mark.parametrize('revision', [source.CUA_REVISION, source.LEGACY_CUA_REVISION, 'wrong'])
@pytest.mark.parametrize('dirty', [False, True])
def test_explicit_checkout_requires_supported_revision_and_clean_inputs(tmp_path, monkeypatch, revision, dirty):
    def git(args, **kwargs):
        assert args[:3] == ['git', '-C', str(tmp_path)]
        return SimpleNamespace(stdout=revision if args[3] == 'rev-parse' else ('scripts/modified.py' if dirty else ''))
    monkeypatch.setattr(source.subprocess, 'run', git)
    if revision == 'wrong' or dirty:
        with pytest.raises(ValueError, match='requires cua-speedrun commit' if revision == 'wrong' else 'local modifications'):
            source.cua(tmp_path)
    else:
        assert source.cua(tmp_path) == tmp_path

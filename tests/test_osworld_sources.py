"""Named OSWorld subsets retain membership, order, source bytes and patches."""
import hashlib
import io
import json
from pathlib import Path
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
    for name in ('osworld-energy50-representative', 'osworld-unanimous-295'):
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
    spec = root / 'benchmarks/osworld-unanimous-295/benchmark-source.yaml'
    selected[0]['source_sha256'] = '0' * 64
    spec.write_text(json.dumps({'tasks': selected}))
    with pytest.raises(ValueError, match='source checksum mismatch'):
        Benchmark('osworld-unanimous-295')

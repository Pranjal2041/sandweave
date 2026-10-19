"""Verify named subsets against the actual pinned private/public sources."""
import hashlib
import json
import os
from pathlib import Path
import pwd
import sys

import pytest

from sandweave import Benchmark
from sandweave.benchmarks import source

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_OSWORLD_SOURCES'),
    reason='explicit network access for pinned public sources required')]


def test_pinned_osworld_subsets_and_full_manifest():
    reference = source.cua()
    read_spec = source.module(reference / 'scripts/build_osworld_subset.py')._read_spec
    full = Benchmark('osworld')
    try:
        full_ids = [task.id for task in full.tasks]
        assert len(full_ids) == len(set(full_ids)) == 369
        reports = {}
        for name, count in [('osworld-energy50-representative', 50), ('osworld-unanimous-295', 295)]:
            spec = read_spec(source.subset_manifest(reference, name))
            assert spec['source_benchmark']['commit'] == source.OSWORLD_REVISION
            bench = Benchmark(name)
            try:
                ids = [task.id for task in bench.tasks]
                assert len(ids) == len(set(ids)) == count
                assert ids == [item['id'] for item in spec['tasks']]
                assert set(ids) <= set(full_ids)
                if count == 295:
                    assert ids == [identity for identity in full_ids if identity in set(ids)]
                patches = 0
                for task, item in zip(bench.tasks, spec['tasks']):
                    directory = Path(task.metadata['directory'])
                    data = (directory / 'source.json').read_bytes()
                    assert hashlib.sha256(data).hexdigest() == item['source_sha256']
                    assert task.instruction == json.loads(data)['instruction']
                    patch = reference / 'scripts/osworld_task_patches' / (directory.name + '.json')
                    if patch.is_file():
                        assert (directory / 'setup-patch.json').read_bytes() == patch.read_bytes()
                        patches += 1
                    else:
                        assert not (directory / 'setup-patch.json').exists()
                reports[name] = {'tasks': count, 'patches': patches,
                    'ordered_ids_sha256': hashlib.sha256(''.join(i + '\n' for i in ids).encode()).hexdigest()}
            finally:
                bench.close()
        print(json.dumps({'cua_revision': source.CUA_REVISION, 'full_tasks': len(full_ids),
                          'subsets': reports}), flush=True)
    finally:
        full.close()


def test_public_setup_relative_download_paths(tmp_path, monkeypatch):
    """Execute the actual upstream setup hook with a local download fixture."""
    reference = source.cua()
    account = pwd.getpwuid(os.getuid())
    desktop_home = tmp_path / 'desktop-home'
    desktop_home.mkdir()
    monkeypatch.setenv('OSWORLD_DESKTOP_USER', account.pw_name)
    monkeypatch.setenv('OSWORLD_DESKTOP_HOME', str(desktop_home))
    monkeypatch.setenv('OSWORLD_X11_DISPLAY', ':99')
    monkeypatch.setenv('HF_XET_CACHE', str(tmp_path / 'hf-cache'))
    monkeypatch.chdir(tmp_path)
    setup = source.module(reference / 'scripts/osworld_shared/osworld_setup.py')
    monkeypatch.setattr(setup, '_fetch', lambda url, path: path.write_bytes(b'fixture download'))
    task = tmp_path / 'source.json'
    task.write_text(json.dumps({'config': [{'type': 'download', 'parameters': {
        'files': [{'url': 'https://example.invalid/fixture', 'path': 'new/nested/task.txt'}]}}]}))
    monkeypatch.setattr(sys, 'argv', ['osworld_setup.py', str(task)])
    setup.main()
    assert Path.cwd() == desktop_home
    output = desktop_home / 'new/nested/task.txt'
    assert output.read_bytes() == b'fixture download'
    for path in (output, output.parent, output.parent.parent):
        assert (path.stat().st_uid, path.stat().st_gid) == (account.pw_uid, account.pw_gid)
    assert not (tmp_path / 'new').exists()

"""Verify named subsets against the actual pinned private/public sources."""
import hashlib
import json
import os
from pathlib import Path

import pytest

from sandweave import Benchmark
from sandweave.benchmarks import source

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_OSWORLD_SOURCES'),
    reason='explicit private repository and network access required')]


def test_pinned_osworld_subsets_and_full_manifest():
    reference = source.cua()
    read_spec = source.module(reference / 'scripts/build_osworld_subset.py')._read_spec
    full = Benchmark('osworld')
    try:
        full_ids = [task.id for task in full.tasks]
        assert len(full_ids) == len(set(full_ids)) == 369
        reports = {}
        for name, count in [('osworld-energy50-representative', 50), ('osworld-unanimous-295', 295)]:
            spec = read_spec(reference / 'benchmarks' / name / 'benchmark-source.yaml')
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

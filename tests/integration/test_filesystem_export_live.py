"""Concurrent large filesystem captures preserve mounts and unrelated service."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import time

import pytest

from sandweave import Memory, Sandbox
from sandweave.sandbox import workspace
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit disposable worker required')]


def test_concurrent_large_exports(tmp_path):
    for path in (workspace.local_parent(), workspace.home()):
        disk = shutil.disk_usage(path.parent)
        if disk.free - 24 * 1024**3 < disk.total * .15:
            pytest.skip('insufficient space for concurrent snapshot qualification')
    recipe = {'extends': 'coding', 'runtime_options': {'docker_data': True}}
    samples, snapshots, report = [], [], {}
    try:
        with Sandbox(template=recipe, memory=Memory('512MiB', '512MiB')) as first, \
             Sandbox(template=recipe, memory=Memory('512MiB', '512MiB')) as second, Sandbox() as control:
            for env in (first, second):
                env.run(argv=['python3', '-c', '''from pathlib import Path
b=bytes(range(256))*4096
for base in ('/workspace','/var/lib/docker'):
    p=Path(base)/'export-probe'; p.mkdir()
    with (p/'large').open('wb') as f:
        for _ in range(512): f.write(b)
    for i in range(8192): (p/str(i)).write_bytes(b[:4096])
'''], timeout=180, check=True)
            expected = first.run('sha256sum /workspace/export-probe/large', check=True).stdout.split()[0]
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(e.snapshot, state='filesystem') for e in (first, second)]
                while not all(f.done() for f in futures):
                    disk = shutil.disk_usage(workspace.local_parent())
                    assert disk.free > disk.total * .15
                    start = time.monotonic()
                    assert control.run('echo responsive', timeout=5, check=True).stdout == 'responsive\n'
                    samples.append(time.monotonic() - start)
                    time.sleep(.05)
                snapshots = [f.result() for f in futures]
            for env in (first, second):
                env.run('rm -rf /workspace/export-probe /var/lib/docker/export-probe', check=True)
        for index, saved in enumerate(snapshots):
            manifest = json.loads((Path(saved.location) / 'snapshot-manifest.json').read_text())
            report[str(index)] = {'pause_seconds': manifest['pause_seconds'],
                                 'export_seconds': manifest['filesystem']['export_seconds']}
            with Sandbox(snapshot=saved) as restored:
                for base in ('/workspace', '/var/lib/docker'):
                    assert restored.run(f'sha256sum {base}/export-probe/large', check=True).stdout.split()[0] == expected
                    restored.run(argv=['python3', '-c', f'''from pathlib import Path
p=Path({base!r})/'export-probe'
assert len(list(p.iterdir()))==8193
assert all((p/str(i)).read_bytes()==bytes(range(256))*16 for i in range(8192))
'''], check=True)
        assert samples and max(samples) < 5
        report['unrelated_commands'] = len(samples)
        report['maximum_command_seconds'] = max(samples)
        report['median_command_seconds'] = sorted(samples)[len(samples)//2]
        (tmp_path / 'export-concurrency.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
    finally:
        connection = local_connection()
        try:
            connection.call('_shutdown_if_idle')
        finally:
            connection.close()

"""Optional cross-version acceptance using an unmodified pre-storage SDK wheel."""
import json
import os
from pathlib import Path
import subprocess
import textwrap

import pytest
from sandweave import Sandbox, Storage
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_LEGACY_PYTHON'), reason='requires a separate pre-0.2.32 interpreter')]


def test_pre_storage_wheel_snapshots_remain_compatible(tmp_path, monkeypatch):
    old = os.environ['SANDWEAVE_LEGACY_PYTHON']
    previous = os.environ['SANDWEAVE_LEGACY_ASSETS']
    current = os.environ['SANDWEAVE_ASSETS']
    home = tmp_path / 'home'; home.mkdir()
    (home / 'config.json').write_text(json.dumps({'assets': previous}))
    env = dict(os.environ, SANDWEAVE_HOME=str(home), SANDWEAVE_ASSETS=previous)
    env.pop('PYTHONPATH', None)
    subprocess.run([old, '-c', textwrap.dedent('''
        import json, sys
        from pathlib import Path
        from importlib.metadata import version
        from sandweave import Sandbox
        from sandweave.sandbox.targets import local_connection
        assert tuple(map(int, version('sandweave').split('.'))) < (0, 2, 32)
        refs = {}
        try:
            with Sandbox() as env:
                env.files.write_text('/workspace/legacy', 'original')
                for state in ('filesystem', 'memory'):
                    refs[state] = str(env.snapshot(state=state))
            Path(sys.argv[1]).write_text(json.dumps(refs))
        finally:
            c = local_connection(); c.call('_shutdown_if_idle'); c.close()
        ''').strip(), str(tmp_path / 'refs.json')], env=env, check=True, timeout=180)
    (home / 'config.json').write_text(json.dumps({'assets': current}))
    monkeypatch.setenv('SANDWEAVE_HOME', str(home))
    refs = json.loads((tmp_path / 'refs.json').read_text())
    try:
        for ref in refs.values():
            with Sandbox(snapshot=ref) as env:
                assert env.spec['storage']['mode'] == 'memory'
                assert env.info['storage']['mode'] == 'memory'
                assert env.files.read_text('/workspace/legacy') == 'original'
        with Sandbox(snapshot=refs['filesystem'], storage=Storage(path=str(tmp_path / 'new-disk'))) as env:
            assert env.info['storage']['mode'] == 'disk'
            assert env.files.read_text('/workspace/legacy') == 'original'
    finally:
        c = local_connection(); c.call('_shutdown_if_idle'); c.close()

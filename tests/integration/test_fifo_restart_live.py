"""Signal interruption must not randomly abort native image-build startup."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from sandweave import Mount, Sandbox

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit disposable worker required')]


def test_fifo_open_signal_restart_matches_linux(tmp_path):
    compiler = shutil.which('cc')
    if compiler is None:
        pytest.skip('C compiler required for the native Linux comparison')
    binary = tmp_path / 'fifo-signal-test'
    source = Path(__file__).with_name('fifo-signal-test.c')
    if not source.is_file():
        source = Path(__file__).resolve().parents[2] / 'sources/fifo-signal-test.c'
    subprocess.run([compiler, '-O2', '-Wall', '-Wextra', '-Werror', str(source), '-o', str(binary)], check=True)
    subprocess.run([str(binary), str(tmp_path / 'native-fifo')], check=True, timeout=15)
    shared = tmp_path / 'shared'
    shared.mkdir()
    with Sandbox(mounts=[Mount(shared, '/host-fifo', read_only=False)]) as env:
        env.files.upload(binary, '/tmp/fifo-signal-test')
        env.run('chmod +x /tmp/fifo-signal-test', check=True)
        for path in ('/tmp/test-fifo', '/workspace/test-fifo', '/host-fifo/test-fifo'):
            result = env.run(argv=['/tmp/fifo-signal-test', path], timeout=15, check=True)
            assert 'obey SA_RESTART' in result.stdout

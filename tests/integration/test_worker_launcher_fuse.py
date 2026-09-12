"""A filesystem's blocked flush must not block unrelated worker launches.

Run with SANDWEAVE_FUSE_INTEGRATION=1 and fusepy installed. This mounts a small
disposable FUSE filesystem; it uses no cloud provider or existing user mount.
"""
from pathlib import Path
import os
import subprocess
import sys
import time

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.environ.get('SANDWEAVE_FUSE_INTEGRATION'),
                                                        reason='explicit disposable FUSE test required')]


@pytest.fixture
def filesystem(tmp_path):
    pytest.importorskip('fuse')
    mount = tmp_path / 'mount'
    mount.mkdir()
    daemon = subprocess.Popen([sys.executable, __file__, str(mount), str(tmp_path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 60
        while not os.path.ismount(mount):
            assert daemon.poll() is None, daemon.communicate()[1].decode()
            assert time.monotonic() < deadline, 'FUSE mount did not become ready'
            time.sleep(.01)
        yield mount, tmp_path
    finally:
        (tmp_path / 'release').touch()
        if os.path.ismount(mount):
            subprocess.run(['fusermount', '-u', str(mount)], check=True)
        if daemon.poll() is None:
            daemon.terminate()
        daemon.wait(timeout=10)


def test_blocked_flush_does_not_block_launches(filesystem):
    mount, state = filesystem
    source = Path(__file__).resolve().parents[2] / 'src'
    result = subprocess.run([sys.executable, '-u', '-c', EXERCISE, str(mount), str(state)],
        env={**os.environ, 'PYTHONPATH': str(source)}, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    print(result.stdout)


@pytest.mark.skipif(not os.environ.get('SANDWEAVE_ASSETS'), reason='prepared runtime required for live FUSE acceptance')
def test_real_sandboxes_start_while_worker_storage_flush_is_blocked(filesystem):
    import json
    from concurrent.futures import ThreadPoolExecutor
    from sandweave import Sandbox, Memory
    from sandweave.sandbox.connection import Connection
    from sandweave.sandbox.targets import Endpoint
    mount, state = filesystem
    marker = state / 'worker.json'
    environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2] / 'src'),
                   'SANDWEAVE_HOME': str(state / 'worker'), 'SANDWEAVE_MEMORY_BUDGET': '4GiB'}
    cpus = sorted(os.sched_getaffinity(0))[:2]
    with (state / 'worker.log').open('wb') as log:
        worker = subprocess.Popen(['taskset', '-c', ','.join(map(str, cpus)), sys.executable,
            '-u', '-c', WORKER, str(marker), str(mount), str(state)],
            env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True)
    connection = None
    try:
        deadline = time.monotonic() + 180
        while not marker.exists() or not (state / 'flushed').exists():
            assert worker.poll() is None, (state / 'worker.log').read_text()
            assert time.monotonic() < deadline, (state / 'worker.log').read_text()
            time.sleep(.02)
        info = json.loads(marker.read_text())
        connection = Connection('127.0.0.1', info['port'], info['token'])
        started = time.monotonic()
        def episode(index):
            with Sandbox(target=Endpoint(info['port'], info['token']),
                         memory=Memory('256MiB', '256MiB')) as env:
                assert env.run(f'printf episode-{index}').stdout == f'episode-{index}'
        with ThreadPoolExecutor(4) as executor:
            list(executor.map(episode, range(4)))
        assert not (state / 'unblocked').exists(), 'sandboxes waited for storage'
        print(json.dumps({'real_sandboxes': 4, 'create_execute_cleanup_seconds': time.monotonic() - started,
                          'native_still_blocked': True}))
    finally:
        (state / 'release').touch()
        if connection is not None:
            for record in connection.call('list'):
                if record['state'] not in ('terminated', 'stopped'):
                    connection.call('terminate', identity=record['id'])
            connection.call('_shutdown_if_idle')
            connection.close()
        elif worker.poll() is None:
            worker.terminate()
        worker.wait(timeout=30)


WORKER = r'''
from pathlib import Path
import sys, threading
from sandweave.sandbox.worker import Worker, serve
from sandweave.sandbox.launcher import _Popen
marker, mount, state = map(Path, sys.argv[1:])
original = Worker.__init__
def initialize(self, root):
    original(self, root)
    def write_image():
        with (mount / 'image').open('wb', buffering=0) as image:
            image.write(b'upload in progress')
            with _Popen(['/bin/true']) as old:
                old.wait()
            (state / 'unblocked').touch()
    threading.Thread(target=write_image, daemon=True).start()
Worker.__init__ = initialize
serve(marker)  # Exercise the ordinary worker entry point and public Sandbox API.
'''


EXERCISE = r'''
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json, os, subprocess, sys, time
from sandweave.sandbox.launcher import install, _Popen
install()  # Before this worker opens storage or starts its request threads.
mount, state = map(Path, sys.argv[1:])
with (mount / 'image').open('wb', buffering=0) as image:
    image.write(b'upload in progress')
    with ThreadPoolExecutor(16) as executor:
        # The old Popen must actually block in FUSE flush. A passing test must
        # reproduce the bug, not merely assert that the new child can execute.
        old = executor.submit(lambda: _Popen(['/bin/true']).wait())
        deadline = time.monotonic() + 5
        while not (state / 'flushed').exists():
            assert not old.done(), 'native spawn did not trigger the blocking flush'
            assert time.monotonic() < deadline, 'native flush was not observed'
            time.sleep(.001)
        assert not old.done()
        started = time.monotonic()
        futures = [executor.submit(subprocess.run, ['/bin/true'], check=True) for _ in range(128)]
        try:
            for future in futures:
                assert future.result(timeout=5).returncode == 0
            elapsed = time.monotonic() - started
            assert not old.done(), 'launches waited for the blocked filesystem'
            print(json.dumps({'launches': 128, 'seconds': elapsed, 'native_still_blocked': True}))
        finally:
            (state / 'release').touch()
        assert old.result(timeout=5) == 0
'''


def mount_filesystem(mount, state):
    import stat
    from fuse import FUSE, Operations
    class SlowClose(Operations):
        def getattr(self, path, fh=None):
            return {'st_mode': (stat.S_IFDIR | 0o700) if path == '/' else (stat.S_IFREG | 0o600),
                    'st_nlink': 2 if path == '/' else 1, 'st_size': 0,
                    'st_uid': os.getuid(), 'st_gid': os.getgid()}
        def readdir(self, path, fh):
            return ['.', '..', 'image']
        def open(self, path, flags):
            return 0
        def truncate(self, path, length, fh=None):
            return 0
        def write(self, path, data, offset, fh):
            return len(data)
        def flush(self, path, fh):
            (state / 'flushed').touch()
            while not (state / 'release').exists():
                time.sleep(.01)
            return 0
    FUSE(SlowClose(), str(mount), foreground=True)


if __name__ == '__main__':
    mount_filesystem(Path(sys.argv[1]), Path(sys.argv[2]))

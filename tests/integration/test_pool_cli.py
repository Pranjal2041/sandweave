"""Bounded clean leases, async cancellation and CLI parity on real sandboxes."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

import pytest
from sandweave import Pool, Sandbox
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit worker required')]


@pytest.fixture(scope='module', autouse=True)
def release_idle_test_worker():
    yield
    connection = local_connection()
    connection.call('_shutdown_if_idle')
    connection.close()


def test_pool_isolation_order_and_bound():
    lock = threading.Lock()
    active, peak = 0, 0
    ids = set()

    def evaluate(env, index):
        nonlocal active, peak
        with lock:
            active += 1; peak = max(peak, active)
            assert env.id not in ids
            ids.add(env.id)
        try:
            assert env.run('test ! -e /workspace/dirty').returncode == 0
            env.files.write_text('/workspace/dirty', str(index))
            time.sleep(.05)
            return index, env.files.read_text('/workspace/dirty')
        finally:
            with lock:
                active -= 1

    with Pool(template='coding', size=3, warm=2) as pool:
        assert len(pool.idle) >= 2
        assert list(pool.map(evaluate, range(8))) == [(i, str(i)) for i in range(8)]
        assert 1 < peak <= 3


def test_async_cancel_stops_owned_command():
    async def exercise():
        async with await Sandbox.create.aio() as env:
            task = asyncio.create_task(env.run.aio('touch /workspace/started; sleep 1; touch /workspace/escaped'))
            deadline = time.monotonic() + 5
            while True:
                try:
                    await env.files.stat.aio('/workspace/started')
                    break
                except FileNotFoundError:
                    assert time.monotonic() < deadline
                    await asyncio.sleep(.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.sleep(1.2)
            assert (await env.run.aio('test ! -e /workspace/escaped')).returncode == 0
    asyncio.run(exercise())


def test_cli_commands_names_streams_and_exit_codes():
    environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2] / 'src')}

    def cli(*args):
        return subprocess.run([sys.executable, '-m', 'sandweave.cli', *args],
                              capture_output=True, text=True, env=environment, timeout=90)

    value = cli('run', '--', "printf 'out'; printf 'err' >&2; exit 7")
    assert (value.returncode, value.stdout, value.stderr) == (7, 'out', 'err')
    value = cli('run', '--argv', '--', 'printf', '%s', '$(touch /oops)')
    assert value.returncode == 0 and value.stdout == '$(touch /oops)'
    name = 'sdk-cli-' + uuid.uuid4().hex
    created = cli('create', '--name', name)
    assert created.returncode == 0, created.stderr
    try:
        value = cli('exec', name, '--', 'echo named')
        assert value.returncode == 0 and value.stdout == 'named\n', value.stderr
    finally:
        stopped = cli('terminate', created.stdout.strip())
        assert stopped.returncode == 0, stopped.stderr

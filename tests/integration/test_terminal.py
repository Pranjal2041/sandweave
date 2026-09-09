"""Guest-owned terminals, controlling TTY state and terminal RAM restoration."""
import asyncio
import json
import os
import subprocess
import sys

import pytest

from sandweave import Sandbox
from sandweave.sandbox.process import Process
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit worker required')]


@pytest.fixture(scope='module', autouse=True)
def release_idle_test_worker():
    yield
    connection = local_connection()
    connection.call('_shutdown_if_idle')
    connection.close()


def reply(process, text):
    process.stdin.write(text + '\n')
    assert process.stdout.readline().strip() == text  # Terminal echo.
    assert process.stdout.readline().strip() == 'reply:' + text
    assert process.wait(timeout=5) == 0


@pytest.mark.parametrize('runtime', ['gvisor', 'apptainer'])
def test_terminal_resize_stdin_and_merged_output(runtime):
    with Sandbox(runtime=runtime) as env:
        code = ('import os,json; print(json.dumps([os.isatty(0),os.isatty(1),os.isatty(2),'
                'list(os.get_terminal_size(0))])); value=input(); '
                'print("reply:"+value); print(json.dumps(list(os.get_terminal_size(0))))')
        process = env.exec(argv=['python', '-u', '-c', code], pty={'rows': 30, 'cols': 100})
        assert json.loads(process.stdout.readline()) == [True, True, True, [100, 30]]
        process.resize(40, 120)
        reply(process, 'terminal €')
        assert json.loads(process.stdout.readline()) == [120, 40]
        assert process.stderr.read() == ''


def test_terminal_open_input_survives_memory_restore():
    with Sandbox() as env:
        process = env.exec(argv=['python', '-u', '-c', 'print("ready"); print("reply:"+input())'], pty=True)
        assert process.stdout.readline().strip() == 'ready'
        saved = env.snapshot(state='memory')
        with Sandbox(snapshot=saved) as clone:
            restored = Process(clone, process.id)
            assert restored.stdout.readline().strip() == 'ready'
            restored.resize(35, 110)
            reply(restored, 'clone')
        reply(process, 'source')


def test_cli_shell_has_controlling_terminal():
    with Sandbox() as env:
        result = subprocess.run([sys.executable, '-m', 'sandweave.cli', 'shell', env.id],
                                input="test -t 0 && echo TERMINAL_OK\nexit\n",
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert '\nTERMINAL_OK\n' in result.stdout.replace('\r', '')
        assert 'no job control' not in result.stdout + result.stderr


def test_async_file_stream_context_and_iteration():
    async def exercise():
        async with await Sandbox.create.aio() as env:
            async with await env.files.open.aio('/workspace/async', 'w') as stream:
                await stream.write.aio('first €\nsecond\n')
            async with await env.files.open.aio('/workspace/async') as stream:
                assert [line async for line in stream] == ['first €\n', 'second\n']
    asyncio.run(exercise())

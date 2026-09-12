"""End-to-end public API acceptance in an explicitly selected disposable worker."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys

import pytest

from sandweave import Sandbox, CommandError, CommandTimeout, OutputLimitExceeded
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit worker required')]


@pytest.fixture(scope='module', autouse=True)
def release_idle_test_worker():
    yield
    connection = local_connection()
    connection.call('_shutdown_if_idle')
    connection.close()


def test_command_strings_files_streams_and_timeouts(tmp_path):
    with Sandbox() as env:
        assert env.run("python -c 'print(2 + 2)'").stdout == '4\n'
        assert env.run("printf 'a b\\n' | tr 'a-z' 'A-Z'").stdout == 'A B\n'
        assert env.run(argv=['python', '-c', 'import sys; print(sys.argv[1])', '$(touch /oops)']).stdout == '$(touch /oops)\n'
        assert env.run('test ! -e /dev/kvm && test ! -e /oops').returncode == 0
        env.files.write_text('/workspace/a.txt', 'hello €\n')
        assert env.files.read_text('/workspace/a.txt') == 'hello €\n'
        env.files.download('/workspace/a.txt', tmp_path/'a.txt')
        env.files.upload(tmp_path/'a.txt', '/workspace/b.txt')
        assert env.run('cmp /workspace/a.txt /workspace/b.txt').returncode == 0
        result = env.run('echo output; echo problem >&2; exit 7')
        assert (result.stdout, result.stderr, result.returncode) == ('output\n', 'problem\n', 7)
        result = env.run("python -c 'print(2 / 0)'")
        assert result.returncode == 1 and not result.stdout
        assert 'ZeroDivisionError' in result.stderr
        with pytest.raises(CommandError) as failed:
            env.run('echo problem >&2; exit 7', check=True)
        assert failed.value.result.returncode == 7
        assert failed.value.result.stderr == 'problem\n'
        assert env.run('exit 9', check=False).returncode == 9
        process = env.exec('cat')
        process.stdin.write('streamed input\n')
        process.stdin.close()
        assert list(process.stdout) == ['streamed input\n']
        assert process.wait(check=True) == 0
        process = env.exec('sleep 2; echo survived')
        with pytest.raises(TimeoutError):
            process.wait(timeout=.05)
        assert process.wait(timeout=5) == 0
        assert process.stdout.read() == 'survived\n'
        with pytest.raises(CommandTimeout):
            env.run('echo partial; sleep 20', timeout=.1)
        with pytest.raises(OutputLimitExceeded):
            env.run("python -c 'print(\"x\" * 65536)'", max_output_bytes=1024)
        cli = subprocess.run([str(Path(sys.executable).parent / 'sandweave'), 'exec', '--no-stdin',
                              env.id, '--', 'echo output; echo problem >&2; exit 7'],
                             capture_output=True, text=True, timeout=30)
        assert (cli.stdout, cli.stderr, cli.returncode) == ('output\n', 'problem\n', 7)


def test_setup_and_borrowed_handle(tmp_path):
    setup = tmp_path/'setup.sh'
    setup.write_text('#!/bin/sh\nprintf prepared > /workspace/setup-result\n')
    with Sandbox(setup=setup) as env:
        with Sandbox.connect(env.id) as borrowed:
            assert borrowed.files.read_text('/workspace/setup-result') == 'prepared'
        assert env.run('echo still-alive').stdout == 'still-alive\n'
        env.pause()
        assert env.status()['state'] == 'paused'
        env.resume()
        assert env.run('echo resumed').stdout == 'resumed\n'


def test_async_create_execute_and_cleanup():
    async def exercise():
        async with await Sandbox.create.aio() as env:
            result = await env.run.aio("python -c 'print(42)'")
            assert result.stdout == '42\n'
            result = await env.run.aio('echo output; echo problem >&2; exit 7')
            assert (result.stdout, result.stderr, result.returncode) == ('output\n', 'problem\n', 7)
            with pytest.raises(CommandError) as failed:
                await env.run.aio('echo problem >&2; exit 7', check=True)
            assert (failed.value.stderr, failed.value.returncode) == ('problem\n', 7)
            with pytest.raises(CommandTimeout):
                await env.run.aio('sleep 20', timeout=.1)
            with pytest.raises(OutputLimitExceeded):
                await env.run.aio("python -c 'print(\"x\" * 65536)'", max_output_bytes=1024)
    asyncio.run(exercise())


def test_create_after_acknowledged_worker_shutdown():
    for _ in range(3):
        with Sandbox() as env:
            assert env.run('printf restarted').stdout == 'restarted'
        connection = local_connection()
        connection.call('_shutdown_if_idle')
        connection.close()

"""Real registry images through the public SDK, including snapshot and pool reuse."""
import json
import os
from pathlib import Path
import time
import uuid

import pytest

from sandweave import Sandbox, Pool
from sandweave.sandbox.process import Process
from sandweave.sandbox.targets import Endpoint

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_IMAGE_INTEGRATION'), reason='explicit disposable image acceptance required')]


def options():
    value = {'startup_timeout': 900}
    if path := os.environ.get('SANDWEAVE_IMAGE_WORKER'):
        metadata = json.loads(Path(path).read_text())
        value['target'] = Endpoint(metadata['port'], metadata['token'])
    return value


def record(env, wall_seconds):
    output = Path(os.environ['SANDWEAVE_IMAGE_INTEGRATION'])
    output.mkdir(parents=True, exist_ok=True)
    (output / (env.id + '.json')).write_text(json.dumps({
        'info': env.info, 'timings': env.timings, 'wall_seconds': wall_seconds}, indent=2))


@pytest.mark.parametrize('image,argv,expected', [
    ('docker://busybox:1.37.0', ['sh', '-c', 'echo busybox; test ! -e /usr/bin/python3'], 'busybox'),
    ('docker://python:3.12-slim', ['python', '-c', 'import sys; print(sys.version_info[:2])'], '(3, 12)'),
    ('docker://gcr.io/distroless/python3-debian12:nonroot',
     ['/usr/bin/python3', '-c', 'import os; print(os.getuid()); print(os.getcwd())'], '65532'),
])
def test_real_image_commands_and_defaults(image, argv, expected):
    start = time.monotonic()
    with Sandbox(image=image, **options()) as env:
        record(env, time.monotonic() - start)
        result = env.run(argv=argv)
        assert result.returncode == 0, result.stderr
        assert expected in result.stdout
        assert env.info['image']['digest'].startswith('sha256:')
        env.files.write_text('/tmp/transfer', 'image file transfer')
        assert env.files.read_text('/tmp/transfer') == 'image file transfer'


def test_template_setup_services_user_override_and_offline(tmp_path):
    script = tmp_path / 'setup.sh'
    script.write_text('#!/bin/sh\nprintf "%s" "$SETUP_VALUE" > /tmp/setup-value\n')
    recipe = tmp_path / 'image.toml'
    recipe.write_text('image="docker://busybox:1.37.0"\nuser="12345:23456"\nworkdir="/tmp"\n'
        '[env]\nSETUP_VALUE="template"\n[setup]\nscript="setup.sh"\n'
        '[services.example]\ncommand="echo ready > /tmp/service-ready; sleep 3600"\n'
        'ready={exec="test -s /tmp/service-ready"}\n')
    with Sandbox(template=recipe, image='docker://python:3.12-slim',
                 env={'SETUP_VALUE': 'constructor'}, network='offline', **options()) as env:
        result = env.run("python -c 'import os; print(os.getuid(),os.getgid(),os.getcwd())'")
        assert result.stdout.strip() == '12345 23456 /tmp'
        assert env.files.read_text('/tmp/setup-value') == 'constructor'
        assert env.files.read_text('/tmp/service-ready') == 'ready\n'
        assert env.run('id -u', user='0').stdout.strip() == '0'
        result = env.run("python -c 'import socket; socket.create_connection((\"1.1.1.1\",443),1)'", timeout=5)
        assert result.returncode != 0
        assert env.run('echo $SETUP_VALUE', env={'SETUP_VALUE': 'command'}).stdout == 'command\n'


def test_filesystem_cache_and_pool_keep_pinned_image(tmp_path):
    key = 'image-setup-' + uuid.uuid4().hex
    setup = tmp_path/'setup.sh'
    setup.write_text('#!/bin/sh\nhead -c16 /dev/urandom | od -An -tx1 > /baseline\n')
    with Sandbox(image='docker://busybox:1.37.0', setup=setup, cache_key=key, **options()) as first:
        baseline = first.files.read_text('/baseline')
        digest = first.info['image']['digest']
    with Sandbox(image='docker://busybox:1.37.0', setup=setup, cache_key=key, **options()) as reused:
        assert reused.files.read_text('/baseline') == baseline
    with Sandbox(cache=key, **options()) as restored:
        assert restored.files.read_text('/baseline') == baseline
        assert restored.info['image']['digest'] == digest
    with Pool(image='docker://busybox:1.37.0', size=2, warm=1, **options()) as pool:
        with pool.acquire() as first:
            first.files.write_text('/episode', 'first')
        with pool.acquire() as second:
            assert second.run('test ! -e /episode').returncode == 0
            assert second.info['image']['digest'] == digest


def test_memory_snapshot_and_pause_keep_application_state():
    with Sandbox(image='docker://python:3.12-slim', **options()) as original:
        process = original.exec("python -u -c 'import uuid; secret=uuid.uuid4().hex; print(\"ready\"); "
                                "print(secret + \":\" + input())'")
        assert process.stdout.readline() == 'ready\n'
        original.pause()
        assert original.status()['state'] == 'paused'
        original.resume()
        saved = original.snapshot(state='memory')
        process.stdin.write('original\n')
        first = process.stdout.readline().strip()
        with Sandbox(snapshot=saved, **options()) as clone:
            copied = Process(clone, process.id)
            assert copied.stdout.readline() == 'ready\n'
            copied.stdin.write('clone\n')
            second = copied.stdout.readline().strip()
            assert first.split(':')[0] == second.split(':')[0]
            assert first.endswith(':original') and second.endswith(':clone')
            copied.stdin.close()
            assert copied.wait(timeout=10) == 0
        process.stdin.close()
        assert process.wait(timeout=10) == 0


def test_cached_launch_timings():
    for image in (None, 'docker://busybox:1.37.0', 'docker://python:3.12-slim'):
        for _ in range(3):
            start = time.monotonic()
            with Sandbox(**({'image': image} if image else {}), **options()) as env:
                record(env, time.monotonic() - start)
                assert env.run('echo ready').stdout == 'ready\n'

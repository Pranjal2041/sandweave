"""Exercise saved files, actual process RAM and safe stop through the public API."""
import os
import uuid

import pytest

from sandweave import Sandbox, CacheMiss, CacheConflict, IncompatibleSnapshot
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


def test_filesystem_revision_independence_and_cache_miss():
    key = 'sdk-files-' + uuid.uuid4().hex
    with pytest.raises(CacheMiss):
        Sandbox(cache='absent-' + uuid.uuid4().hex)
    with Sandbox() as original:
        original.files.write_text('/workspace/value', 'baseline')
        baseline = original.cache(key)
        assert baseline.state == 'filesystem'
        assert baseline.verification['status'] in ('pending', 'running', 'passed')
        original.files.write_text('/workspace/value', 'source changed')
        with Sandbox(cache=key) as first, Sandbox(cache=baseline) as second:
            assert first.files.read_text('/workspace/value') == 'baseline'
            first.files.write_text('/workspace/value', 'first changed')
            assert second.files.read_text('/workspace/value') == 'baseline'
            assert original.files.read_text('/workspace/value') == 'source changed'
        changed = original.cache(key)
        assert changed.id != baseline.id
        with Sandbox(cache=baseline) as pinned, Sandbox(cache=key) as latest:
            assert pinned.files.read_text('/workspace/value') == 'baseline'
            assert latest.files.read_text('/workspace/value') == 'source changed'
    assert baseline.verify()['status'] == 'passed'


def test_memory_checkpoint_preserves_running_process_and_stdin():
    with Sandbox() as original:
        process = original.exec(argv=['python', '-u', '-c',
            'import uuid; secret=uuid.uuid4().hex; print("ready"); '
            'print(secret + ":" + input()); print(secret + ":" + input())'])
        assert process.stdout.readline() == 'ready\n'
        baseline = original.snapshot(state='memory')
        process.stdin.write('source\n')
        source = process.stdout.readline().strip()
        with Sandbox(snapshot=baseline) as clone:
            restored = Process(clone, process.id)
            assert restored.stdout.readline() == 'ready\n'
            restored.stdin.write('clone\n')
            copied = restored.stdout.readline().strip()
            assert copied.split(':')[0] == source.split(':')[0]
            assert copied.endswith(':clone') and source.endswith(':source')
            restored.stdin.write('done\n'); restored.stdin.close()
            assert restored.wait(timeout=5) == 0
        process.stdin.write('done\n'); process.stdin.close()
        assert process.wait(timeout=5) == 0
        with pytest.raises(IncompatibleSnapshot):
            Sandbox(snapshot=baseline, cpu=2)


def test_stop_and_failed_save_keeps_source_alive():
    with Sandbox() as original:
        original.files.write_text('/workspace/persist', 'saved')
        with pytest.raises(ValueError):
            original.stop(state='invalid')
        assert original.run('echo alive').stdout == 'alive\n'
        saved = original.stop()
        assert original.status()['state'] == 'stopped'
    with Sandbox(snapshot=saved) as restored:
        assert restored.files.read_text('/workspace/persist') == 'saved'


def test_preparation_reuse_invalidation_and_provenance(tmp_path):
    key = 'sdk-build-' + uuid.uuid4().hex
    script = tmp_path / 'prepare.sh'
    script.write_text('#!/bin/sh\npython -c "import uuid; print(uuid.uuid4().hex)" > /workspace/build\n')
    with Sandbox(setup=script, cache_key=key) as first:
        first_value = first.files.read_text('/workspace/build')
        with pytest.raises(CacheConflict):
            first.cache(key)
    with Sandbox(setup=script, cache_key=key) as reused:
        assert reused.files.read_text('/workspace/build') == first_value
    script.write_text(script.read_text() + 'echo changed >> /workspace/build\n')
    with Sandbox(setup=script, cache_key=key) as changed:
        value = changed.files.read_text('/workspace/build')
        assert value != first_value and value.endswith('changed\n')
    with Sandbox(cache=key) as cached:
        assert cached.files.read_text('/workspace/build') == value

import asyncio
import dataclasses
from pathlib import Path

import pytest

from sandweave import CPU, GPU, Memory, Network
from sandweave.sandbox.asyncio import dualmethod, dualclassmethod
from sandweave.sandbox.resources import memory_bytes, normalize
from sandweave.sandbox.wire import encode, decode
from sandweave.templates.resolve import Template, fingerprint


def test_binary_protocol_preserves_arbitrary_user_mapping():
    value = {'message': {'blobs': [b'\0\xff', b''], 'nothing': None}, 'path': ['a', 2]}
    assert decode(encode(value)) == value
    assert decode(encode(b'\0raw')) == b'\0raw'
    with pytest.raises(ValueError):
        decode(encode(value) + b'extra')
    with pytest.raises(ValueError):
        decode(encode(value)[:-1])


@pytest.mark.parametrize('value', [True, -1, 0, float('nan'), float('inf')])
def test_invalid_cpu_is_rejected(value):
    with pytest.raises(ValueError):
        CPU(value)


def test_resources_preserve_distinct_budgets_and_opt_in():
    result = normalize(cpu=CPU(2, weight=200, quota=1.5), memory=Memory('4GiB', '1GiB'))
    assert result['cpu']['quota'] == 1.5
    assert memory_bytes(result['memory']['guest']) == 4 * 1024**3
    assert memory_bytes(result['memory']['runtime']) == 1024**3
    with pytest.raises(ValueError):
        GPU(sm_chunks=2)
    with pytest.raises(ValueError):
        Network('offline', ('1.1.1.1/32',))


def test_template_inheritance_and_setup_fingerprint(tmp_path):
    script = tmp_path / 'setup.sh'
    script.write_text('#!/bin/sh\necho first\n')
    recipe = tmp_path / 'template.toml'
    recipe.write_text('extends="gnome@1"\n[setup]\nscript="setup.sh"\n[resources]\ncpu=2\n')
    before = Template(recipe).resolve()
    assert before['resources']['cpu'] == 2
    assert before['resources']['memory'] == '8GiB'
    assert before['capabilities']['desktop']['backend'] == 'xvnc'
    script.write_text('#!/bin/sh\necho second\n')
    assert fingerprint(before) != fingerprint(Template(recipe).resolve())
    recipe.write_text('extends="template.toml"\n')
    with pytest.raises(ValueError, match='cycle'):
        Template(recipe).resolve()


def test_sync_and_async_descriptor_bind_same_instance_and_class():
    class Example:
        @dualclassmethod
        def create(cls, value):
            result = cls()
            result.value = value
            return result

        @dualmethod
        def get(self):
            return self.value

    async def run():
        instance = await Example.create.aio(42)
        assert await instance.get.aio() == 42
        assert instance.get() == 42
    asyncio.run(run())


def test_worker_identity_separates_cpu_and_gpu_eligibility(monkeypatch):
    from sandweave.sandbox import workspace
    from sandweave.sandbox.workspace import worker_key
    import os
    affinity = sorted(os.sched_getaffinity(0))
    monkeypatch.setattr(workspace, 'asset_identity', lambda: 'isolated-test-assets')
    monkeypatch.setattr(os, 'sched_getaffinity', lambda _: {affinity[0]})
    first = worker_key()
    monkeypatch.setenv('CUDA_VISIBLE_DEVICES', 'different-eligible-device')
    assert worker_key() != first
    second = worker_key()
    monkeypatch.setattr(os, 'sched_getaffinity', lambda _: {affinity[0], affinity[0]+1})
    assert worker_key() != second


def test_workspace_lock_serializes_threads_and_releases_after_errors(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import time
    from sandweave.sandbox.workspace import locked
    path = tmp_path / 'shared.lock'
    active = 0
    def enter(index):
        nonlocal active
        try:
            with locked(path):
                assert active == 0
                active += 1
                time.sleep(.001)
                active -= 1
                if index % 3 == 0:
                    raise RuntimeError('exercise exceptional release')
        except RuntimeError:
            pass
        return index
    with ThreadPoolExecutor(32) as executor:
        assert list(executor.map(enter, range(128))) == list(range(128))
    with locked(path):
        assert active == 0


@pytest.mark.parametrize('method', ['read', 'readline'])
def test_stream_drains_bytes_arriving_between_empty_read_and_exit(method):
    from types import SimpleNamespace
    from sandweave.sandbox.process import OutputStream
    chunks = iter([b'', b'last output\n', b'', b''])
    process = SimpleNamespace(id='test', poll=lambda: 0,
                              sandbox=SimpleNamespace(_call=lambda *a, **k: next(chunks)))
    assert getattr(OutputStream(process, 'stdout'), method)() == 'last output\n'

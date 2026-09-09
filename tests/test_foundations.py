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

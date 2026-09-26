"""Storage selection, portable restores, and private backing directory ownership."""
import copy
import json
from pathlib import Path

import pytest

from sandweave import Storage
from sandweave.sandbox.sandbox import definition
from sandweave.sandbox.runtimes.gvisor.driver import Runtime


def test_storage_defaults_and_template_overrides(tmp_path):
    assert definition()['spec']['storage'] == {'mode': 'disk', 'path': None}
    template = {'resources': {'storage': {'path': '/data/writable'}}}
    assert definition(template=template)['spec']['storage'] == {'mode': 'disk', 'path': '/data/writable'}
    spec = definition(template=template, storage='memory')['spec']
    assert spec['storage'] == {'mode': 'memory', 'path': None}
    runtime = Runtime.__new__(Runtime)
    runtime.root = tmp_path
    options = runtime.options(definition(storage=Storage(path='/data/writable'))['spec'])
    assert options[options.index('--storage') + 1] == 'disk'
    assert options[options.index('--storage-path') + 1] == '/data/writable'


@pytest.mark.parametrize('kwargs', [{'mode': 'invalid'}, {'mode': 'memory', 'path': '/data'},
    {'path': 'relative'}, {'path': '/data/../bad'}, {'path': '/data:bad'}, {'path': '/data\nbad'}])
def test_invalid_storage(kwargs):
    with pytest.raises(ValueError):
        Storage(**kwargs)


@pytest.mark.parametrize('legacy', [False, True])
def test_restore_keeps_saved_mode_but_can_relocate(monkeypatch, legacy):
    saved = definition(storage=Storage(path='/old/disk'))['spec']
    if legacy:
        saved.pop('storage')
    original = copy.deepcopy(saved)
    class Connection:
        def call(self, operation, **kwargs):
            return {'reference': 'snap-pinned', 'spec': saved}
        def close(self):
            pass
    monkeypatch.setattr('sandweave.sandbox.sandbox.connect', lambda target: Connection())
    assert definition(snapshot='x')['spec']['storage']['mode'] == ('memory' if legacy else 'disk')
    assert definition(snapshot='x', storage=Storage(path='/new/disk'))['spec']['storage']['path'] == '/new/disk'
    assert saved == original


def test_disk_mounts_preserve_snapshot_inventory_and_cleanup(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / 'scripts'))
    import filesystem_storage as storage
    import filesystem_snapshot as snapshot
    spec = {'mounts': [
        {'type': 'tmpfs', 'source': 'tmpfs', 'destination': '/run', 'options': ['mode=0755']},
        {'type': 'tmpfs', 'source': 'tmpfs', 'destination': '/data', 'options': ['mode=0750']},
    ], 'annotations': {}}
    original = copy.deepcopy(spec['mounts'])
    logs = tmp_path / 'logs'; logs.mkdir()
    parent = tmp_path / 'custom'
    directory = storage.configure(spec, 'disk', parent, logs)
    assert directory.parent == parent and directory.stat().st_mode & 0o777 == 0o700
    assert spec['mounts'][0] == original[0]
    assert spec['mounts'][1]['source'] == '/sandbox-storage/mount-0'
    assert storage.persistent_mounts(spec) == original
    assert snapshot.inventory(spec, '1 0 0:1 / / rw - overlay none rw\n2 1 0:2 / /data rw - tmpfs none rw') == [
        {'destination': '/data', 'options': ['mode=0750'], 'file': 'mount-0.tar'}]
    other = parent / 'keep'; other.write_text('unrelated')
    storage.cleanup(logs)
    storage.cleanup(logs)
    assert not directory.exists() and other.read_text() == 'unrelated'
    storage.configure(spec, 'memory', parent, logs)
    assert spec['mounts'] == original
    assert not any(k.startswith('dev.gvisor.spec.mount.') for k in spec['annotations'])


def test_cli_storage():
    from argparse import ArgumentParser
    from sandweave.cli import creation_options, creation
    parser = ArgumentParser()
    creation_options(parser)
    assert 'storage' not in creation(parser.parse_args([]))
    assert creation(parser.parse_args(['--storage-path', '/data/files']))['storage'] == Storage(path='/data/files')
    assert creation(parser.parse_args(['--storage', 'memory']))['storage'] == Storage('memory')


def test_client_rejects_worker_that_would_ignore_disk_storage(monkeypatch):
    from types import SimpleNamespace
    from sandweave import Sandbox, UnsupportedFeature
    calls = []
    def call(operation, **kwargs):
        calls.append(operation)
        assert operation == 'ping'
        return {}
    connection = SimpleNamespace(call=call, close=lambda: None)
    connection.clone = lambda **kwargs: connection
    monkeypatch.setattr('sandweave.sandbox.sandbox.connect', lambda *a, **kw: connection)
    with pytest.raises(UnsupportedFeature, match='disk-backed storage'):
        Sandbox()
    assert calls == ['ping']

"""Only a single-owner build volume may cache metadata without host revalidation."""
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_exclusive_mount_hints_do_not_leak_into_shared_mounts(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / 'scripts'))
    import external_mounts
    (tmp_path / 'data').mkdir()
    (tmp_path / 'owners').mkdir()
    mount = {'source': str(tmp_path), 'destination': '/build', 'read_only': False,
             '_private_volume': True, '_exclusive': True}
    spec = {'mounts': [], 'annotations': {}}
    external_mounts.configure(spec, external_mounts.normalize([mount]))
    assert spec['annotations']['dev.sandweave.private-volume-exclusive./build'] == 'true'
    external_mounts.configure(spec, external_mounts.normalize([{**mount, '_exclusive': False}]))
    assert not any(key.startswith('dev.sandweave.private-volume-exclusive.') for key in spec['annotations'])
    with pytest.raises(ValueError, match='owned private volume'):
        external_mounts.normalize([{**mount, '_private_volume': False}])


@pytest.mark.parametrize('duplicate_mount', [False, True])
def test_exclusive_volume_rejects_multiple_mounts_before_allocating(duplicate_mount):
    from sandweave.sandbox.services import Services
    services = Services.__new__(Services)
    services.worker = SimpleNamespace(write=lambda record: pytest.fail('allocated invalid volume'))
    mount = {'name': 'build', 'target': '/build'}
    child = {'request': {'spec': {'runtime': 'gvisor'}}, 'volumes': [mount]}
    spec = {'service_volumes': {'build': {'exclusive': True}},
            'service_mounts': [mount, mount] if duplicate_mount else [mount],
            'services': {} if duplicate_mount else {'child': child}}
    with pytest.raises(ValueError, match='exactly one mount'):
        services.prepare({'spec': spec})


def test_exclusive_volume_rejects_host_side_import():
    from sandweave.sandbox.services import Services
    services = Services.__new__(Services)
    services.worker = SimpleNamespace(read=lambda _: {'spec': {'service_volumes': {'build': {'exclusive': True}}}})
    with pytest.raises(ValueError, match='owning sandbox'):
        services.import_volume('builder', 'build', 'file')

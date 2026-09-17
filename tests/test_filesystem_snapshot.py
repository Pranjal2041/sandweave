"""Filesystem coverage follows backing storage, not the number of mount views."""
from pathlib import Path

import pytest


@pytest.fixture
def fs(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / 'scripts'))
    import filesystem_snapshot
    return filesystem_snapshot


ROOT = '1 0 0:1 / / rw - overlay none rw'
SPEC = {'mounts': [{'destination': '/data', 'type': 'tmpfs', 'options': ['mode=0750']}]}
DATA = '2 1 0:2 / /data rw - tmpfs none rw'


@pytest.mark.parametrize('extra', [
    '3 1 0:1 /workspace /workspace rw shared:4 - overlay none rw',
    '3 2 0:2 / /data rw shared:4 - tmpfs none rw',
    '3 2 0:2 /nested /data/nested rw - tmpfs none rw',
    '3 2 0:2 / /data rw - tmpfs none rw\n4 3 0:2 / /data rw - tmpfs none rw',
    '3 2 0:2 /nested /data/nested rw - tmpfs none rw\n4 3 0:2 /nested/child /data/nested/child rw - tmpfs none rw',
    '3 1 0:1 /with\\040space /with\\040space rw - overlay none rw',
])
def test_self_binds_do_not_duplicate_backing_filesystem(fs, extra):
    # Mountinfo order is not a traversal guarantee.
    records = [*extra.splitlines(), DATA, ROOT]
    assert fs.inventory(SPEC, '\n'.join(records)) == [
        {'destination': '/data', 'options': ['mode=0750'], 'file': 'mount-0.tar'}]


@pytest.mark.parametrize('extra', [
    '3 1 0:1 /elsewhere /workspace rw - overlay none rw',
    '3 1 0:9 /workspace /workspace rw - overlay none rw',
    '3 2 0:2 /elsewhere /data/nested rw - tmpfs none rw',
    '3 2 0:9 / /data rw - tmpfs none rw',
    '3 99 0:2 / /data rw - tmpfs none rw',
    '3 1 0:1 /elsewhere /var/lib/docker/alias rw - overlay none rw',
])
def test_distinct_or_unaccounted_storage_is_not_a_self_bind(fs, extra):
    with pytest.raises(ValueError):
        fs.inventory(SPEC, '\n'.join([ROOT, DATA, extra]))


def test_missing_persistent_mount_is_still_rejected(fs):
    with pytest.raises(ValueError, match='differs'):
        fs.inventory(SPEC, ROOT + '\n3 1 0:1 /data /data rw - overlay none rw')


def test_two_distinct_filesystems_at_same_path_remain_rejected(fs):
    with pytest.raises(ValueError, match='stacked'):
        fs.inventory(SPEC, '\n'.join([ROOT, DATA, '3 2 0:3 / /data rw - tmpfs none rw']))

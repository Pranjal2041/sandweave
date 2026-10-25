"""Admission respects the process's effective cgroup, not just job variables."""
import pytest

from sandweave.sandbox import admission


@pytest.mark.parametrize('cached', [False, True])
def test_pending_image_import_does_not_break_unrelated_admission(tmp_path, cached):
    from types import SimpleNamespace
    record = {'id': 'builder', 'state': 'ready', 'image_imports': ['pending'],
              'runtime_status': {'status': 'running'}}
    def status(identity):
        assert identity == 'builder', 'uncreated importer has no runtime record'
        return record['runtime_status']
    worker = SimpleNamespace(path=lambda identity: tmp_path / identity,
                             runtime=SimpleNamespace(status=status))
    records = {'builder': record} if cached else None
    assert admission.live(worker, record, records=records)
    record['runtime_status']['status'] = 'stopped'
    assert not admission.live(worker, record, records=records)



def test_admission_reads_only_active_records_and_asks_each_gone_runtime_once(tmp_path):
    from types import SimpleNamespace
    from sandweave.sandbox.sandbox import definition
    spec = definition()['spec']
    records = {
        'ready': {'id': 'ready', 'state': 'ready', 'spec': spec},
        'failed': {'id': 'failed', 'state': 'failed', 'spec': spec},
        'kept': {'id': 'kept', 'state': 'failed', 'spec': spec},
        **{f'old-{i}': {'id': f'old-{i}', 'state': 'terminated', 'spec': spec} for i in range(100)}}
    reads, statuses = [], []
    def status(identity):
        statuses.append(identity)
        return {'status': 'running' if identity == 'kept' else 'stopped'}
    def read(identity):
        reads.append(identity)
        return records[identity]
    worker = SimpleNamespace(records=tmp_path, read=read, path=lambda identity: tmp_path / identity,
                             runtime=SimpleNamespace(status=status), active={'ready', 'failed', 'kept'},
                             admission_gone=set(), memory_budget=2**50)
    one = admission.reservation(spec)
    assert admission.admit(worker, spec)['memory_reserved'] == one
    first = list(statuses)
    # A ready record holds its reservation without a runtime query; a runtime seen gone is not asked again.
    assert sorted(first) == ['failed', 'kept'] and set(reads) == {'ready', 'failed', 'kept'}
    admission.admit(worker, spec)
    assert statuses[len(first):] == ['kept']
    worker.active.discard('failed')
    admission.admit(worker, spec)
    assert worker.admission_gone == set()
    budget = 3 * one
    worker.memory_budget = budget  # ready and kept are charged; failed is not.
    admission.admit(worker, spec)
    worker.memory_budget = budget - 1
    with pytest.raises(admission.ResourceUnavailable):
        admission.admit(worker, spec)

@pytest.mark.parametrize('version', [1, 2])
def test_narrower_ancestor_limits_an_unlimited_child(tmp_path, version):
    proc = tmp_path / 'proc'
    (proc / 'self').mkdir(parents=True)
    mount = tmp_path / 'memory cgroup'
    (mount / 'job/step').mkdir(parents=True)
    escaped = str(mount).replace(' ', r'\040')
    filesystem = 'cgroup2 cgroup rw' if version == 2 else 'cgroup cgroup rw,memory'
    (proc / 'self/mountinfo').write_text(f'1 0 0:1 /allocation {escaped} rw - {filesystem}\n')
    membership = '0::' if version == 2 else '5:memory:'
    (proc / 'self/cgroup').write_text(membership + '/allocation/job/step\n')
    filename = 'memory.max' if version == 2 else 'memory.limit_in_bytes'
    unlimited = 'max' if version == 2 else str(2**63 - 4096)
    (mount / filename).write_text(str(64 * 1024**3))
    (mount / 'job' / filename).write_text(str(32 * 1024**3))
    (mount / 'job/step' / filename).write_text(unlimited)
    assert admission.cgroup_limit(proc) == 32 * 1024**3
    (mount / 'job/step' / filename).write_text(str(8 * 1024**3))
    assert admission.cgroup_limit(proc) == 8 * 1024**3


def test_explicit_budget_cannot_exceed_cgroup(monkeypatch):
    monkeypatch.setattr(admission, 'cgroup_limit', lambda: 33554432000)
    monkeypatch.setenv('SANDWEAVE_MEMORY_BUDGET', '64GiB')
    assert admission.budget() == 33554432000
    monkeypatch.setenv('SANDWEAVE_MEMORY_BUDGET', '8GiB')
    assert admission.budget() == 8 * 1024**3


def test_no_visible_cgroup_preserves_explicit_budget(tmp_path, monkeypatch):
    assert admission.cgroup_limit(tmp_path) is None
    monkeypatch.setattr(admission, 'cgroup_limit', lambda: None)
    monkeypatch.setenv('SANDWEAVE_MEMORY_BUDGET', '4GiB')
    assert admission.budget() == 4 * 1024**3


def test_cgroup_namespace_root_and_v1_hidden_ancestor(tmp_path):
    proc = tmp_path / 'proc'
    (proc / 'self').mkdir(parents=True)
    mount = tmp_path / 'cgroup'
    mount.mkdir()
    (proc / 'self/mountinfo').write_text(f'1 0 0:1 /job/step {mount} rw - cgroup cgroup rw,memory\n')
    (proc / 'self/cgroup').write_text('5:memory:/\n')
    (mount / 'memory.limit_in_bytes').write_text(str(2**63 - 4096))
    (mount / 'memory.stat').write_text('hierarchical_memory_limit 1073741824\n')
    assert admission.cgroup_limit(proc) == 1024**3


def test_v1_does_not_apply_a_parent_with_hierarchy_disabled(tmp_path):
    proc = tmp_path / 'proc'
    (proc / 'self').mkdir(parents=True)
    mount = tmp_path / 'cgroup'
    (mount / 'child').mkdir(parents=True)
    (proc / 'self/mountinfo').write_text(f'1 0 0:1 / {mount} rw - cgroup cgroup rw,memory\n')
    (proc / 'self/cgroup').write_text('5:memory:/child\n')
    (mount / 'memory.limit_in_bytes').write_text(str(1024**3))
    (mount / 'child/memory.limit_in_bytes').write_text(str(2**63 - 4096))
    (mount / 'child/memory.stat').write_text('hierarchical_memory_limit ' + str(2**63 - 4096) + '\n')
    assert admission.cgroup_limit(proc) is None

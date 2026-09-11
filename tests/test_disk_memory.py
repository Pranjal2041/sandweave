"""Disk backing must not silently consume the overflow allowance as host RAM."""
import argparse
import importlib
import json
from pathlib import Path
import sys

import pytest

from sandweave import Memory
from sandweave.cli import creation, creation_options
from sandweave.sandbox.admission import reservation
from sandweave.sandbox.resources import normalize, restore_resources
from sandweave.sandbox.sandbox import definition


def test_disk_memory_is_separate_from_admission_and_guest_total(tmp_path):
    from sandweave.sandbox.runtimes.gvisor.driver import Runtime
    spec = definition(memory=Memory('4GiB', '512MiB', '16GiB', str(tmp_path)))['spec']
    assert reservation(spec) == 4608 * 1024**2
    runtime = object.__new__(Runtime)
    runtime.root = tmp_path
    runtime.gpu = lambda *a, **kw: None
    options = runtime.options(spec)
    assert options[options.index('--memory-mib') + 1] == '20480'
    assert options[options.index('--ram-mib') + 1] == '4096'
    assert options[options.index('--disk-path') + 1] == str(tmp_path)
    assert normalize()['memory'] == {'guest': '1GiB', 'runtime': '512MiB'}


@pytest.mark.parametrize('fields', [dict(disk='1GiB'), dict(disk_path='/scratch'),
    dict(disk='0GiB', disk_path='/scratch'), dict(disk=True, disk_path='/scratch'),
    dict(disk='1GiB', disk_path='relative'), dict(disk='1GiB', disk_path='/scratch/../tmp'),
    dict(disk='1GiB', disk_path='/scratch:rw'), dict(disk='1GiB', disk_path='/scratch,bad')])
def test_invalid_disk_memory_fails_before_connecting(fields):
    with pytest.raises(ValueError):
        Memory(**fields)


def test_template_cli_and_restore_keep_explicit_worker_path():
    parser = argparse.ArgumentParser()
    creation_options(parser)
    options = creation(parser.parse_args(['--memory', '4GiB', '--disk-memory', '16GiB',
                                          '--disk-path', '/scratch/memory with spaces']))
    expected = Memory('4GiB', '512MiB', '16GiB', '/scratch/memory with spaces')
    assert options['memory'] == expected
    spec = definition(template={'resources': {'memory': normalize(memory=expected)['memory']}})['spec']
    assert spec['resources']['memory'] == normalize(memory=expected)['memory']
    other = normalize(memory=Memory('4GiB', '512MiB', '16GiB', '/other/disk'))
    assert restore_resources(other) == restore_resources(spec['resources'])
    assert other['memory']['disk_path'] == '/other/disk'
    assert restore_resources(normalize(memory=Memory('8GiB', '512MiB', '16GiB', '/other'))) != restore_resources(other)


@pytest.fixture
def host_memory(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / 'scripts'))
    return importlib.import_module('disk_memory')


def test_kernel_limit_verification_rejects_unenforced_or_swap_cap(tmp_path, monkeypatch, host_memory):
    group = tmp_path / 'sandbox'
    group.mkdir()
    (tmp_path / 'memory.max').write_text('max')
    (group / 'memory.max').write_text('max')
    (group / 'memory.swap.max').write_text('0')
    monkeypatch.setattr(host_memory, 'cgroup', lambda: (group, tmp_path))
    with pytest.raises(RuntimeError, match='did not enforce'):
        host_memory.verify_limit(1024)
    for wrong in ('512', '2048'):
        (group / 'memory.max').write_text(wrong)
        with pytest.raises(RuntimeError):
            host_memory.verify_limit(1024)
    (group / 'memory.max').write_text('1024')
    assert host_memory.verify_limit(1024) == group
    (group / 'memory.swap.max').write_text('max')
    with pytest.raises(RuntimeError, match='swap disabled'):
        host_memory.verify_limit(1024)


def test_delegation_uses_only_an_existing_enabled_subtree(tmp_path, monkeypatch, host_memory):
    current = tmp_path / 'delegated'
    current.mkdir()
    (current / 'cgroup.subtree_control').write_text('memory pids')
    (current / 'memory.max').write_text('4096')
    (current / 'cgroup.procs').write_text('12345')
    monkeypatch.setattr(host_memory, 'cgroup', lambda: (current, tmp_path))
    group = host_memory.delegate(1024)
    assert group.parent == current
    assert (group / 'memory.max').read_text() == '1024'
    assert (group / 'memory.swap.max').read_text() == '0'
    assert (group / 'memory.oom.group').read_text() == '1'
    assert (current / 'memory.max').read_text() == '4096'
    assert (current / 'cgroup.procs').read_text() == '12345'
    (current / 'cgroup.subtree_control').write_text('pids')
    assert host_memory.delegate(1024) is None
    assert (current / 'cgroup.subtree_control').read_text() == 'pids'


def test_cleanup_never_deletes_user_files_or_replacement(tmp_path, host_memory):
    logs = tmp_path / 'logs'
    logs.mkdir()
    directory = tmp_path / 'sandweave-memory-random'
    directory.mkdir()
    record = {'directory': str(directory), 'directory_inode': directory.stat().st_ino,
              'directory_device': directory.stat().st_dev}
    (logs / 'disk-memory.json').write_text(json.dumps(record))
    (directory / 'user-file').write_text('preserve')
    with pytest.raises(OSError):
        host_memory.cleanup(logs)
    assert (directory / 'user-file').read_text() == 'preserve'
    directory.rename(tmp_path / 'preserved')
    directory.mkdir()
    host_memory.cleanup(logs)
    assert directory.exists()
    (tmp_path / 'preserved/user-file').unlink()
    directory.rmdir()
    (tmp_path / 'preserved').rename(directory)
    host_memory.cleanup(logs)
    host_memory.cleanup(logs)
    assert not directory.exists()


def test_slurm_shares_cpus_without_modifying_allocation(monkeypatch, host_memory):
    monkeypatch.setenv('SLURM_JOB_ID', '12345')
    monkeypatch.setattr(host_memory.shutil, 'which', lambda _: '/usr/bin/srun')
    monkeypatch.setattr(host_memory, 'allocation_cpus', lambda *a: 128)
    command = host_memory.slurm_command(4608 * 1024**2, list(range(64)))
    assert '--overlap' in command and '--exact' in command
    assert '--cpus-per-task=128' in command and '--mem=4608M' in command
    assert '--cpu-bind=mask_cpu:0xffffffffffffffff' in command
    assert '--gres=none' in command and '--jobid=12345' in command
    monkeypatch.delenv('SLURM_JOB_ID')
    with pytest.raises(RuntimeError, match='neither'):
        host_memory.slurm_command(1024**3, [0])


def test_engine_feature_probe_uses_flag_listing(tmp_path, monkeypatch, host_memory):
    from types import SimpleNamespace
    from sandweave.sandbox.runtimes.gvisor import engine
    import runtime_store
    monkeypatch.setattr(runtime_store, 'validate', lambda *a: None)
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        # runsc --help lists subcommands; only `flags` describes config flags.
        return SimpleNamespace(stdout=b'  -app-memory-directory string\n' if command[-1] == 'flags' else b'Usage: runsc')
    monkeypatch.setattr(engine.subprocess, 'run', run)
    assert engine.supports_disk(tmp_path, {'path': 'tools/runtime-builds/test'})
    assert calls[0][-1] == 'flags'

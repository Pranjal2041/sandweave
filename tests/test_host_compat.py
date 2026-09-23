import copy
from pathlib import Path

import pytest

from sandweave.sandbox import workspace


@pytest.fixture
def host(monkeypatch):
    module = workspace.host_compat()
    state = {'uid': 1001, 'gid': 1002, 'caps': 0,
             'map': '0 0 4294967295\n', 'missing': set()}
    monkeypatch.setattr(module.os, 'geteuid', lambda: state['uid'])
    monkeypatch.setattr(module.os, 'getuid', lambda: state['uid'])
    monkeypatch.setattr(module.os, 'getgid', lambda: state['gid'])
    class HostPath:
        def __init__(self, name):
            self.name = name
        def read_text(self):
            if self.name == '/proc/self/status':
                return f"CapEff:\t{state['caps']:x}\n"
            if self.name == '/proc/self/uid_map':
                return state['map']
            raise AssertionError(self.name)
        def exists(self):
            return self.name not in state['missing']
    monkeypatch.setattr(module, 'Path', HostPath)
    return module, state


def test_unprivileged_host_keeps_user_namespace(host):
    module, _ = host
    assert module.apptainer_options() == ['--userns']


def test_root_without_mount_capability_still_needs_user_namespace(host):
    module, state = host
    state['uid'] = 0
    assert module.apptainer_options() == ['--userns']


def test_root_with_mount_capability_uses_existing_authority(host):
    module, state = host
    state.update(uid=0, caps=1 << 21)
    assert module.apptainer_options() == []


def test_missing_optional_bind_preserves_explicit_mount_exclusions(host):
    module, state = host
    state['missing'] = {'/etc/localtime'}
    assert module.apptainer_options(no_mount=('tmp', 'home', 'cwd')) == [
        '--userns', '--no-mount', 'tmp,home,cwd,/etc/localtime']


def test_restored_spec_uses_destination_host_identity(host):
    module, state = host
    original = {'linux': {'namespaces': [{'type': 'network'}, {'type': 'user'}],
                         'uidMappings': [{'containerID': 0, 'hostID': 8765, 'size': 1}]}}
    spec = copy.deepcopy(original)
    state.update(uid=0, gid=0, caps=1 << 21, map='0 231072 65536\n')
    module.configure_user_namespace(spec)
    assert spec['linux']['namespaces'] == [
        {'type': 'network'}, {'type': 'user', 'path': '/proc/self/ns/user'}]
    assert 'uidMappings' not in spec['linux'] and 'gidMappings' not in spec['linux']
    # A checkpoint made there remains restorable on an ordinary unprivileged host.
    state.update(uid=1001, gid=1002, caps=0)
    module.configure_user_namespace(spec)
    assert spec['linux']['namespaces'] == original['linux']['namespaces']
    assert spec['linux']['uidMappings'] == [{'containerID': 0, 'hostID': 1001, 'size': 1}]
    assert spec['linux']['gidMappings'] == [{'containerID': 0, 'hostID': 1002, 'size': 1}]


def test_initial_host_root_does_not_share_user_namespace(host):
    module, state = host
    state.update(uid=0, gid=0, caps=1 << 21)
    spec = {'linux': {'namespaces': [{'type': 'user'}]}}
    module.configure_user_namespace(spec)
    assert spec['linux']['namespaces'] == [{'type': 'user'}]
    assert spec['linux']['uidMappings'] == [{'containerID': 0, 'hostID': 0, 'size': 1}]


def test_container_host_image_is_published_once_for_concurrent_callers(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import time

    module = workspace.host_compat()
    monkeypatch.setattr(module, 'reuse_user_namespace', lambda: True)
    image = tmp_path / 'host.sif'
    image.write_bytes(b'immutable SIF')
    alias = tmp_path / 'worker-host.sif'
    alias.hardlink_to(image)
    calls = []

    def extract(image, root, apptainer):
        calls.append(image)
        root.mkdir()
        time.sleep(.03)  # A second caller must never see an unfinished directory.
        (root / 'complete').touch()

    monkeypatch.setattr(module, '_extract_host_image', extract)
    with ThreadPoolExecutor(max_workers=6) as executor:
        paths = list(executor.map(lambda path: module.apptainer_image(path, directory=tmp_path),
                                  [image, alias] * 3))
    assert len(calls) == 1
    assert len(set(paths)) == 1 and (paths[0] / 'complete').is_file()
    assert module.apptainer_image(image, directory=tmp_path) == paths[0]
    assert len(calls) == 1


def test_failed_extraction_is_not_reused(tmp_path, monkeypatch):
    module = workspace.host_compat()
    monkeypatch.setattr(module, 'reuse_user_namespace', lambda: True)
    image = tmp_path / 'host.sif'
    image.write_bytes(b'immutable SIF')

    def failed(image, root, apptainer):
        root.mkdir()
        raise RuntimeError('extraction failed')

    monkeypatch.setattr(module, '_extract_host_image', failed)
    with pytest.raises(RuntimeError, match='extraction failed'):
        module.apptainer_image(image, directory=tmp_path)
    assert not any(path.is_dir() for path in (tmp_path / 'host-images').iterdir())


@pytest.mark.parametrize('bundled', [True, False])
def test_extraction_selects_primary_partition_without_launching_container(tmp_path, monkeypatch, bundled):
    import subprocess
    from types import SimpleNamespace

    module = workspace.host_compat()
    image = tmp_path / 'host.sif'
    image.write_bytes(b'header' + b'hsqs' + b'payload')
    native = tmp_path / 'libexec/apptainer/bin/unsquashfs'
    native.parent.mkdir(parents=True)
    native.write_text('test extractor')
    native.chmod(0o700 if bundled else 0o600)
    monkeypatch.setattr('shutil.which', lambda name: '/usr/bin/unsquashfs')
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[1:] == ['buildcfg']:
            output = f'LIBEXECDIR={tmp_path / "libexec"}\n'
        elif command[1:3] == ['sif', 'list']:
            output = ('ID | GROUP | LINK | SIF POSITION (start-end) | TYPE\n'
                      '1 | 1 | NONE | 0-6 | JSON.Generic\n'
                      '2 | 1 | NONE | 0-6 | FS (Squashfs/Data/amd64)\n'
                      '7 | 1 | NONE | 6-17 | FS (Squashfs/*System/amd64)\n')
        else:
            assert command[0] == (str(native) if bundled else '/usr/bin/unsquashfs')
            assert command[command.index('-offset') + 1] == '6'
            assert command[-2:] == [str(image), 'dev']
            assert '-user-xattrs' in command and '-excludes' in command
            Path(command[command.index('-d') + 1]).mkdir()
            output = ''
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(subprocess, 'run', run)
    module._extract_host_image(image, tmp_path / 'rootfs', 'apptainer')
    assert (tmp_path / 'rootfs/dev').is_dir()
    assert len(commands) == 3


@pytest.mark.parametrize('position,magic', [('0-99', b'hsqs'), ('0-4', b'bad!')])
def test_extraction_rejects_invalid_partition_before_writing(tmp_path, monkeypatch, position, magic):
    import subprocess
    from types import SimpleNamespace

    module = workspace.host_compat()
    image = tmp_path / 'host.sif'
    image.write_bytes(magic)
    monkeypatch.setattr('shutil.which', lambda name: '/usr/bin/unsquashfs')

    def run(command, **kwargs):
        if command[1:] == ['buildcfg']:
            return SimpleNamespace(returncode=0, stdout='')
        assert command[1:3] == ['sif', 'list']
        return SimpleNamespace(returncode=0, stdout=f'1 | 1 | NONE | {position} | FS (Squashfs/*System/amd64)')

    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='invalid SquashFS partition'):
        module._extract_host_image(image, tmp_path / 'rootfs', 'apptainer')
    assert not (tmp_path / 'rootfs').exists()

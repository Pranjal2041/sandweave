import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile

import pytest

from sandweave.sandbox.sandbox import definition
from sandweave.templates import images
from sandweave.templates.layers import Filesystem, unpack
from sandweave.templates.registry import reference, Registry
from sandweave.templates.resolve import Template


def layer(path, entries):
    with tarfile.open(path, 'w') as stream:
        for name, value in entries:
            if isinstance(value, tarfile.TarInfo):
                info, content = value, None
                info.name = name
            else:
                content = value.encode()
                info = tarfile.TarInfo(name)
                info.size = len(content)
            stream.addfile(info, io.BytesIO(content) if content is not None else None)
    return path


def link(target, kind=tarfile.LNKTYPE):
    info = tarfile.TarInfo()
    info.type, info.linkname = kind, target
    return info


def contents(filesystem, tmp_path):
    destination = tmp_path / 'merged.tar'
    filesystem.write(destination)
    with tarfile.open(destination) as stream:
        return {m.name: stream.extractfile(m).read() if m.isfile() or m.islnk() else m
                for m in stream}


def test_image_and_template_are_independent_and_explicit_image_wins(tmp_path):
    setup = tmp_path / 'install.sh'
    setup.write_text('echo installed')
    recipe = tmp_path / 'custom.toml'
    recipe.write_text('image="docker://python:3.12-slim"\n[setup]\nscript="install.sh"\n'
                      '[services.server]\ncommand="python -m http.server"\n')
    original = definition(template=recipe)['spec']
    overridden = definition(template=recipe, image='docker://python:3.13-slim')['spec']
    assert original['image']['reference'] == 'docker://python:3.12-slim'
    assert overridden['image']['reference'] == 'docker://python:3.13-slim'
    assert original['template']['services'] == overridden['template']['services']
    assert original['template']['setup_steps'] == overridden['template']['setup_steps']
    assert definition(image='docker://busybox')['spec']['template']['_user_explicit'] is False
    assert definition()['spec']['template']['user'] == 'root'


def test_image_configuration_and_restored_spec(monkeypatch):
    image = {'reference': 'docker://busybox', 'digest': 'sha256:' + 'a'*64,
             'settings': {'user': '123:456', 'workdir': '/app', 'env': {'A': 'base', 'B': 'base'}}}
    monkeypatch.setattr(images, 'prepare', lambda *a, **kw: image)
    spec, _ = images.configure(definition(image='docker://busybox', env={'B': 'override'})['spec'], '.')
    assert spec['template']['user'] == '123:456'
    assert spec['template']['workdir'] == '/app'
    assert spec['env'] == {'A': 'base', 'B': 'override'}
    spec, _ = images.configure(definition(template={'user': 'root', 'workdir': '/code'},
                                         image='docker://busybox')['spec'], '.')
    assert spec['template']['user'] == 'root'
    assert spec['template']['workdir'] == '/code'
    class Connection:
        def call(self, *a, **kw):
            return {'reference': 'snap-123', 'spec': spec}
        def close(self):
            pass
    monkeypatch.setattr('sandweave.sandbox.sandbox.connect', lambda *a: Connection())
    restored = definition(cache='sample')['spec']
    assert restored['image'] == spec['image']
    assert restored['env'] == spec['env']


@pytest.mark.parametrize('value', ['python:3.12', 'docker://', 'docker://x/../y',
                                  'docker://registry/x?bad', 'docker://x@sha256:bad'])
def test_invalid_image_is_rejected_before_connecting(value):
    with pytest.raises(ValueError):
        definition(image=value)


def test_registry_normalization_and_refresh():
    assert reference('docker://python:3.12') == ('registry-1.docker.io', 'library/python', '3.12')
    assert reference('docker://docker.io/library/python') == ('registry-1.docker.io', 'library/python', 'latest')
    assert reference('docker://example.com:5000/org/image:tag') == ('example.com:5000', 'org/image', 'tag')
    definition(template={'image': 'docker://python'}, refresh=True)
    with pytest.raises(ValueError, match='saved source'):
        definition(cache='old', image='docker://new')


def test_whiteouts_opaque_directories_and_lower_hardlink_identity(tmp_path):
    filesystem = Filesystem()
    filesystem.apply(layer(tmp_path/'a.tar', [('dir/old', 'old'), ('dir/removed', 'removed'),
                                            ('target', 'original'), ('alias', link('target'))]))
    filesystem.apply(layer(tmp_path/'b.tar', [('dir/new', 'new'), ('dir/.wh..wh..opq', ''),
                                            ('target', 'replacement'), ('.wh.absent', '')]))
    result = contents(filesystem, tmp_path)
    assert result['dir/new'] == b'new'
    assert 'dir/old' not in result and 'dir/removed' not in result
    assert result['target'] == b'replacement'
    assert result['alias'] == b'original'
    assert not any('.wh.' in name for name in result)


def test_symlink_parents_stay_inside_guest_and_preserve_ownership(tmp_path):
    directory = tarfile.TarInfo()
    directory.type, directory.uid, directory.gid, directory.mode = tarfile.DIRTYPE, 123, 456, 0o2750
    directory.pax_headers = {'SCHILY.xattr.user.example': 'value'}
    filesystem = Filesystem()
    filesystem.apply(layer(tmp_path/'a.tar', [('usr/bin', directory), ('bin', link('/usr/bin', tarfile.SYMTYPE))]))
    filesystem.apply(layer(tmp_path/'b.tar', [('bin/tool', 'binary')]))
    result = contents(filesystem, tmp_path)
    assert result['usr/bin/tool'] == b'binary'
    assert result['bin'].linkname == '/usr/bin'
    assert (result['usr/bin'].uid, result['usr/bin'].gid, result['usr/bin'].mode) == (123, 456, 0o2750)
    assert result['usr/bin'].pax_headers['SCHILY.xattr.user.example'] == 'value'


def test_root_opaque_whiteout_retains_root_metadata(tmp_path):
    root = tarfile.TarInfo()
    root.type, root.mode, root.uid, root.gid = tarfile.DIRTYPE, 0o750, 123, 456
    filesystem = Filesystem()
    filesystem.apply(layer(tmp_path/'lower.tar', [('.', root), ('old', 'removed')]))
    filesystem.apply(layer(tmp_path/'upper.tar', [('.wh..wh..opq', ''), ('new', 'retained')]))
    result = contents(filesystem, tmp_path)
    assert 'old' not in result and result['new'] == b'retained'
    assert (result['.'].mode, result['.'].uid, result['.'].gid) == (0o750, 123, 456)


@pytest.mark.parametrize('name', ['/host', '../host', 'safe/../../host'])
def test_archive_traversal_rejected_without_host_extraction(tmp_path, name):
    with pytest.raises(ValueError, match='unsafe path'):
        Filesystem().apply(layer(tmp_path/'bad.tar', [(name, 'bad')]))


def test_forward_hardlinks_and_regular_file_replacing_directory(tmp_path):
    filesystem = Filesystem()
    filesystem.apply(layer(tmp_path/'a.tar', [('first', link('second')), ('second', 'same'), ('dir/child', 'old')]))
    filesystem.apply(layer(tmp_path/'b.tar', [('dir', 'now a file')]))
    result = contents(filesystem, tmp_path)
    assert result['first'] == result['second'] == b'same'
    assert result['dir'] == b'now a file'
    assert 'dir/child' not in result


def test_layer_digest_verification(tmp_path):
    source, output = tmp_path/'layer.gz', tmp_path/'layer.tar'
    source.write_bytes(gzip.compress(b'content'))
    expected = 'sha256:' + hashlib.sha256(b'content').hexdigest()
    unpack(source, output, 'application/vnd.oci.image.layer.v1.tar+gzip', expected)
    assert output.read_bytes() == b'content'
    with pytest.raises(ValueError, match='digest mismatch'):
        unpack(source, output, 'application/vnd.oci.image.layer.v1.tar+gzip', 'sha256:'+'0'*64)


def test_registry_rejects_tampered_manifest_and_wrong_platform(tmp_path, monkeypatch):
    registry = Registry('docker://test', tmp_path)
    body = json.dumps({'schemaVersion': 2}).encode()
    monkeypatch.setattr(registry, 'open', lambda *a: io.BytesIO(body))
    with pytest.raises(ValueError, match='digest mismatch'):
        registry.manifest('sha256:' + '0'*64)
    monkeypatch.setattr(registry, 'manifest', lambda *a: ('sha256:'+'0'*64, {
        'manifests': [{'platform': {'os': 'linux', 'architecture': 'arm64'}}]}))
    with pytest.raises(ValueError, match='linux/amd64'):
        registry.resolve()


def test_numeric_user_without_passwd_and_explicit_group(monkeypatch):
    from sandweave.sandbox import guest_agent
    def missing(*a):
        raise KeyError()
    monkeypatch.setattr(guest_agent.pwd, 'getpwuid', missing)
    uid, gid, home, login, groups = guest_agent.user_account('12345:6789')
    assert (uid, gid, groups) == (12345, 6789, [])
    assert home == '/'


def test_prepared_image_reuses_digest_without_registry_or_repacking(tmp_path, monkeypatch):
    from sandweave import bootstrap
    counters = {'resolve': 0, 'download': 0, 'pack': 0}
    data, root = tmp_path/'data', tmp_path/'worker'
    root.mkdir()
    monkeypatch.setenv('SANDWEAVE_HOME', str(data))
    control = layer(tmp_path/'control.tar', [('python/bin/python3.13', 'private executable')])
    compressed = tmp_path/'control.gz'
    compressed.write_bytes(gzip.compress(control.read_bytes()))
    class FakeRegistry:
        def __init__(self, *args):
            pass
        def resolve(self):
            counters['resolve'] += 1
            return {'digest': 'sha256:'+'a'*64, 'platform': 'linux/amd64',
                    'manifest': {'layers': []},
                    'config': {'rootfs': {'diff_ids': []}, 'config': {}}}
    class FakeBuilder:
        def __init__(self, *args):
            pass
        def erofs(self, root, source, destination):
            counters['pack'] += 1
            destination.write_bytes(source.read_bytes())
    def download(*a, **kw):
        counters['download'] += 1
        return compressed
    monkeypatch.setattr(images, 'Registry', FakeRegistry)
    monkeypatch.setattr(bootstrap, 'Builder', FakeBuilder)
    monkeypatch.setattr(bootstrap, 'download', download)
    monkeypatch.setattr(images, 'control_archive', lambda source, destination:
                        destination.write_bytes(control.read_bytes()))
    first = images.prepare('docker://example.com/image', root)
    assert images.prepare('docker://example.com/image', root) == first
    assert counters == {'resolve': 1, 'download': 1, 'pack': 1}
    assert images.prepare('docker://example.com/image', root, refresh=True) == first
    assert counters == {'resolve': 2, 'download': 1, 'pack': 1}
    assert (root / first['base_image']).is_file()
    assert first['settings']['workdir'] == '/'
    for pool in ('pool-' + 'a'*32, 'pool-' + 'b'*32):
        private = images.prepare('docker://example.com/image', root, pool=pool)
        assert private['base_image'].startswith('images/pools/' + pool + '/')
        assert (root / private['base_image']).read_bytes() == (root / first['base_image']).read_bytes()
    assert counters == {'resolve': 2, 'download': 1, 'pack': 1}


def test_redirect_does_not_forward_registry_token_to_blob_host():
    from urllib.request import Request
    from sandweave.templates.registry import RegistryRedirect
    request = Request('https://registry.example.org/v2/blob', headers={'Authorization': 'Bearer private'})
    redirected = RegistryRedirect().redirect_request(request, None, 302, 'Found', {},
                                                      'https://storage.example.org/blob')
    assert not redirected.has_header('Authorization')
    with pytest.raises(ValueError, match='non-HTTPS'):
        RegistryRedirect().redirect_request(request, None, 302, 'Found', {}, 'http://registry.example.org/blob')


def test_control_python_rejects_external_loader():
    import struct
    data = bytearray(120)
    data[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<H', data, 18, 62)
    struct.pack_into('<Q', data, 32, 64)
    struct.pack_into('<HH', data, 54, 56, 1)
    struct.pack_into('<I', data, 64, 1)  # PT_LOAD
    images.verify_static_python(data)
    struct.pack_into('<I', data, 64, 3)  # PT_INTERP
    with pytest.raises(ValueError, match='no dynamic dependencies'):
        images.verify_static_python(data)

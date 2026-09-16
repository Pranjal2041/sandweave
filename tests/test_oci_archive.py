"""OCI imports authenticate bytes before making a built image available."""
import hashlib
import io
import json
import tarfile

import pytest

from sandweave.templates.oci_archive import Archive


def make_archive(tmp_path, monkeypatch, *, tamper=None, extra=()):
    from sandweave.templates import oci_archive
    monkeypatch.setattr(oci_archive, 'home', lambda: tmp_path)
    entries = {'oci-layout': b'{"imageLayoutVersion":"1.0.0"}'}
    def blob(data):
        data = data if isinstance(data, bytes) else json.dumps(data).encode()
        digest = hashlib.sha256(data).hexdigest()
        entries['blobs/sha256/' + digest] = data
        return {'digest': 'sha256:' + digest, 'size': len(data)}
    layer = blob(b'example layer bytes')
    config = blob({'architecture': 'amd64', 'os': 'linux',
                   'rootfs': {'type': 'layers', 'diff_ids': [layer['digest']]}})
    manifest = blob({'schemaVersion': 2, 'config': config, 'layers': [layer]})
    entries['index.json'] = json.dumps({'schemaVersion': 2, 'manifests': [manifest]}).encode()
    if tamper:
        entries['blobs/sha256/' + {'config': config, 'layer': layer}[tamper]['digest'][7:]] = b'x' * {'config': config, 'layer': layer}[tamper]['size']
    directory = tmp_path / 'images/archives'
    directory.mkdir(parents=True)
    path = directory / ('a' * 64 + '.tar')
    with tarfile.open(path, 'w') as archive:
        for name, content in [*entries.items(), *extra]:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return Archive({'format': 'oci', 'sha256': 'a' * 64}, tmp_path / 'blobs')


def test_valid_archive_resolves_and_reads_authenticated_layer(tmp_path, monkeypatch):
    archive = make_archive(tmp_path, monkeypatch)
    resolved = archive.resolve()
    assert resolved['platform'] == 'linux/amd64'
    descriptor = resolved['manifest']['layers'][0]
    path = archive.blob(descriptor)
    assert path.read_bytes() == b'example layer bytes'
    path.write_bytes(b'x' * descriptor['size'])
    assert archive.blob(descriptor).read_bytes() == b'example layer bytes'


@pytest.mark.parametrize('tamper', ['config', 'layer'])
def test_corrupt_image_never_publishes_a_layer(tmp_path, monkeypatch, tamper):
    archive = make_archive(tmp_path, monkeypatch, tamper=tamper)
    with pytest.raises(ValueError, match='digest'):
        resolved = archive.resolve()
        archive.blob(resolved['manifest']['layers'][0])
    assert not [p for p in (tmp_path / 'blobs').iterdir() if p.suffix != '.lock']


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'index.json'])
def test_rejects_escaping_and_ambiguous_members(tmp_path, monkeypatch, name):
    with pytest.raises(ValueError, match='path'):
        make_archive(tmp_path, monkeypatch, extra=[(name, b'{}')])

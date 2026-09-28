"""Build a standard local OCI archive from a small real Linux image for acceptance."""
import hashlib
import io
import json
import tarfile

from sandweave.templates.registry import Registry


def archive(path):
    registry = Registry('docker://busybox:1.37.0', path.parent/'blobs')
    selected = registry.resolve()
    config = selected['config']
    config.setdefault('config', {}).update(User='123:123', WorkingDir='/local-image',
                                         Env=['PATH=/usr/bin:/bin', 'LOCAL_IMAGE=yes'])
    with tarfile.open(path, 'w') as output:
        def entry(name, data):
            raw = json.dumps(data).encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            output.addfile(info, io.BytesIO(raw))
            return {'digest': 'sha256:' + hashlib.sha256(raw).hexdigest(), 'size': len(raw)}
        def blob(data, media):
            raw = json.dumps(data).encode()
            value = entry('blobs/sha256/' + hashlib.sha256(raw).hexdigest(), data)
            return {**value, 'mediaType': media}
        config_ref = blob(config, 'application/vnd.oci.image.config.v1+json')
        manifest = {**selected['manifest'], 'config': config_ref}
        manifest_ref = blob(manifest, 'application/vnd.oci.image.manifest.v1+json')
        for descriptor in manifest['layers']:
            output.add(registry.blob(descriptor), arcname='blobs/sha256/' + descriptor['digest'][7:])
        entry('index.json', {'schemaVersion': 2, 'manifests': [manifest_ref]})
        entry('oci-layout', {'imageLayoutVersion': '1.0.0'})
    return path

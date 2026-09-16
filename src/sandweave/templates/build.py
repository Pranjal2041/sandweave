"""Build original Dockerfiles with BuildKit and publish native filesystem bases."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import tarfile
import tempfile
import time

from ..sandbox.errors import CacheMiss, UnsupportedFeature
from ..sandbox.sandbox import Sandbox, definition
from ..sandbox.snapshots import SnapshotRef
from ..sandbox.targets import connect

BUILDER_IMAGE = 'docker://moby/buildkit:v0.25.1'


def context_archive(directory, destination):
    directory = Path(directory).resolve(strict=True)
    with tarfile.open(destination, 'w', format=tarfile.PAX_FORMAT) as archive:
        archive.add(directory, arcname='.', recursive=True)


def remote_context(value):
    return str(value).startswith(('https://', 'http://', 'git://', 'ssh://', 'git@'))


def secret_value(source):
    if source.get('file'):
        return Path(source['file']).read_bytes()
    if 'environment' in source:
        return os.environ[source['environment']].encode()
    if 'content' in source:
        return source['content'].encode()
    raise ValueError('build secrets require a file, environment, or content source')


def build(directory, *, template=None, target=None, dockerfile='Dockerfile',
          dockerfile_inline=None, build_args=None, stage=None, force=False, timeout=600, log=None,
          additional_contexts=None, secrets=None, network=None, pull=False, labels=None,
          platform='linux/amd64'):
    """Return a portable Sandweave snapshot; no task is executed inside the builder."""
    remote = str(directory) if remote_context(directory) else None
    directory = Path(directory) if remote is None else None
    if platform != 'linux/amd64':
        raise UnsupportedFeature('the native Sandweave runtime requires linux/amd64 images: ' + platform)
    if network not in (None, 'default', 'host', 'none'):
        raise ValueError('invalid BuildKit network mode: ' + str(network))
    if remote and dockerfile_inline is not None:
        raise ValueError('an inline Dockerfile requires a local build context')
    dockerfile_path = directory / dockerfile if directory is not None else None
    recipe = (dockerfile_inline.encode() if dockerfile_inline is not None else
              dockerfile_path.read_bytes() if dockerfile_path is not None else None)
    ignore_path = dockerfile_path.with_name(dockerfile_path.name + '.dockerignore') if dockerfile_path else None
    ignore = ignore_path.read_bytes() if dockerfile_inline is None and ignore_path and ignore_path.is_file() else None
    template = template or {'name': 'image', 'command_shell': '/bin/sh'}
    with tempfile.TemporaryDirectory(prefix='sandweave-build-') as temporary:
        context = Path(temporary) / 'context.tar'
        if directory is not None:
            context_archive(directory, context)
            with context.open('rb') as stream:
                checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
        else:
            checksum = remote
        extra, extra_checksums = {}, {}
        for name, value in (additional_contexts or {}).items():
            if not re.fullmatch(r'[a-zA-Z0-9_.-]+', name):
                raise ValueError('invalid additional build context name: ' + name)
            if remote_context(value) or str(value).startswith(('docker-image://', 'oci-layout://')):
                extra[name] = str(value)
                extra_checksums[name] = str(value)
            else:
                path = Path(temporary) / ('extra-' + name + '.tar')
                context_archive(value, path)
                extra[name] = path
                with path.open('rb') as stream:
                    extra_checksums[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
        secret_data = {name: secret_value(value) for name, value in (secrets or {}).items()}
        if any(not re.fullmatch(r'[a-zA-Z0-9_.-]+', name) for name in secret_data):
            raise ValueError('invalid build secret ID')
        arguments = {name: value if value is not None else os.environ.get(name, '')
                     for name, value in (build_args or {}).items()}
        settings = {'context': checksum, 'dockerfile': hashlib.sha256(recipe).hexdigest() if recipe else dockerfile, 'args': arguments,
                    'ignore': hashlib.sha256(ignore).hexdigest() if ignore is not None else None,
                    'stage': stage, 'builder': BUILDER_IMAGE, 'template': template, 'format': 3,
                    'extra': extra_checksums, 'secrets': {name: hashlib.sha256(value).hexdigest() for name, value in secret_data.items()},
                    'network': network, 'labels': labels, 'platform': platform}
        key = 'image-build-' + hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()
        connection = connect(target)
        try:
            if not force and not pull and not remote and not any(isinstance(value, str) for value in extra.values()):
                try:
                    saved = connection.call('snapshot_info', reference=key)
                    return SnapshotRef.from_record(saved, connection.clone())
                except (CacheMiss, FileNotFoundError):
                    pass
            if not connection.call('ping').get('oci_image_import') and not hasattr(connection, 'config'):
                raise UnsupportedFeature('Dockerfile builds require an updated Sandweave worker')
        finally:
            connection.close()
        started = time.monotonic()
        request = definition(image=BUILDER_IMAGE, target=target, cpu=2, memory='4GiB',
            startup_timeout=timeout, template={'runtime_options': {'nftables': True, 'cgroup': 'v1'}})
        # The importer briefly boots the resulting filesystem to capture a
        # portable base. Admit that runtime with the builder, before placement.
        request['spec']['_image_import_memory'] = 384 * 1024**2
        builder = Sandbox._from_definition(request, target)
        try:
            from .credentials import build_configuration
            auth = build_configuration()
            if auth['auths']:
                builder.run('mkdir -p /root/.docker && chmod 700 /root/.docker', check=True)
                builder.files.write_text('/root/.docker/config.json', json.dumps(auth))
                builder.run('chmod 600 /root/.docker/config.json', check=True)
            builder.run('mkdir -p /build/context /build/recipe /build/secrets && chmod 700 /build/secrets', check=True)
            if directory is not None:
                builder.files.upload(context, '/tmp/context.tar')
                builder.run('tar -xf /tmp/context.tar -C /build/context', check=True)
                builder.files.write_bytes('/build/recipe/Dockerfile', recipe)
            if ignore is not None:
                builder.files.write_bytes('/build/recipe/Dockerfile.dockerignore', ignore)
            process = builder.exec('buildkitd --root /var/lib/buildkit --oci-worker-snapshotter=native '
                '--oci-worker-net=host --containerd-worker=false --allow-insecure-entitlement network.host > /tmp/buildkit.log 2>&1')
            while builder.run('buildctl debug workers >/dev/null 2>&1').returncode:
                if process.poll() is not None:
                    raise RuntimeError('BuildKit exited: ' + builder.files.read_text('/tmp/buildkit.log'))
                if time.monotonic() - started > timeout:
                    raise TimeoutError('BuildKit startup exceeded the image build timeout')
                time.sleep(.1)
            command = ['buildctl', 'build', '--frontend=dockerfile.v0',
                '--opt', 'filename=' + (dockerfile if remote else 'Dockerfile'), '--opt', 'platform=' + platform,
                '--output', 'type=oci,dest=/tmp/image.tar', '--progress=plain']
            if remote:
                command += ['--opt', 'context=' + remote]
            else:
                command += ['--local', 'context=/build/context', '--local', 'dockerfile=/build/recipe']
            for name, value in extra.items():
                if isinstance(value, Path):
                    destination = '/build/extra-' + name
                    builder.files.upload(value, '/tmp/extra.tar')
                    builder.run(shlex.join(['mkdir', '-p', destination]) + ' && ' +
                        shlex.join(['tar', '-xf', '/tmp/extra.tar', '-C', destination]), check=True)
                    command += ['--local', 'extra-' + name + '=' + destination, '--opt', 'context:' + name + '=local:extra-' + name]
                else:
                    command += ['--opt', 'context:' + name + '=' + value]
            for name, value in secret_data.items():
                destination = '/build/secrets/' + name
                builder.files.write_bytes(destination, value)
                builder.run(shlex.join(['chmod', '600', destination]), check=True)
                command += ['--secret', 'id=' + name + ',src=' + destination]
            if network is not None:
                command += ['--opt', 'force-network-mode=' + network]
                if network == 'host':
                    command += ['--allow', 'network.host']
            if pull:
                command += ['--opt', 'image-resolve-mode=pull']
            for name, value in (labels or {}).items():
                command += ['--opt', 'label:' + name + '=' + str(value)]
            for name, value in arguments.items():
                command.extend(['--opt', 'build-arg:' + name + '=' + str(value)])
            if stage:
                command.extend(['--opt', 'target=' + stage])
            if force:
                command.extend(['--no-cache'])
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError('Image build timeout expired before execution')
            result = builder.run(shlex.join(command), timeout=remaining)
            if log is not None:
                Path(log).parent.mkdir(parents=True, exist_ok=True)
                Path(log).write_text(result.stdout + result.stderr)
            if result.returncode:
                raise RuntimeError('Dockerfile build failed: ' + result.stderr)
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError('Image build timeout expired before import')
            saved = builder._call('image_capture', path='/tmp/image.tar', template=template, timeout=remaining)
            # Registering an alias happens through the ordinary snapshot API,
            # including the controller's compare-and-swap publication rules.
            # The immutable revision remains usable independently of the alias.
            reference = SnapshotRef.from_record(saved, builder._connection.clone())
            from ..weave.client import ClusterConnection
            if isinstance(builder._connection, ClusterConnection):
                route = builder._connection._route(builder.id)
                expected = builder._connection.call('snapshot_alias', key=key)
                builder._connection.control.call('snapshot_register', identity=route['id'],
                    reference=reference.id, key=key, expected=expected)
            else:
                builder._call('image_alias', reference=reference.id, key=key)
            return reference
        finally:
            try:
                builder.terminate()
            finally:
                builder.close()

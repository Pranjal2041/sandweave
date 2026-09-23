"""Host capability handling shared by setup and the runtime launchers."""
import os
from pathlib import Path
import sys


def mount_privileged():
    """Whether root already has mount authority in its current user namespace."""
    if os.geteuid() != 0:
        return False
    try:
        fields = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines()
                      if ':' in line)
        return bool(int(fields['CapEff'].strip(), 16) & (1 << 21))
    except (OSError, KeyError, ValueError):
        return False


def apptainer_options(*, no_mount=()):
    # Root with mount authority does not need another user namespace. Keep
    # Apptainer's user namespace path for ordinary unprivileged callers.
    options = [] if mount_privileged() else ['--userns']
    skipped = list(no_mount)
    # These optional default binds are absent on some minimal Linux images.
    for name in ('/etc/localtime', '/etc/hosts'):
        if not Path(name).exists():
            skipped.append(name)
    if skipped:
        options += ['--no-mount', ','.join(dict.fromkeys(skipped))]
    return options


def reuse_user_namespace():
    if not mount_privileged():
        return False
    try:
        mapping = [[int(value) for value in line.split()]
                   for line in Path('/proc/self/uid_map').read_text().splitlines()]
        return bool(mapping) and mapping != [[0, 0, 4294967295]]
    except (OSError, ValueError):
        return False


def _extract_host_image(image, destination, apptainer):
    """Extract our trusted tools SIF without starting an extraction container.

    Apptainer's build command starts an internal container whose environment
    drops NO_MOUNT settings. Use its SIF metadata and installed unsquashfs;
    this needs no optional host bind sources or changes to Apptainer config.
    """
    import shutil
    import subprocess

    def run(command):
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if result.returncode:
            raise RuntimeError('Preparing the Apptainer host image failed:\n' + result.stdout[-8000:])
        return result.stdout

    config = dict(line.split('=', 1) for line in run([apptainer, 'buildcfg']).splitlines() if '=' in line)
    bundled = Path(config['LIBEXECDIR']) / 'apptainer/bin/unsquashfs' if config.get('LIBEXECDIR') else None
    extractor = str(bundled) if bundled and os.access(bundled, os.X_OK) else shutil.which('unsquashfs')
    if not extractor:
        raise RuntimeError('Apptainer has no available unsquashfs extractor')

    partitions = []
    for line in run([apptainer, 'sif', 'list', str(image)]).splitlines():
        fields = [part.strip() for part in line.split('|')]
        if len(fields) == 5 and fields[4].startswith('FS (Squashfs/*System/'):
            start, end = map(int, fields[3].split('-'))
            partitions.append((start, end))
    if len(partitions) != 1:
        raise RuntimeError('Apptainer host image must contain one primary SquashFS filesystem')
    start, end = partitions[0]
    if not 0 <= start < end <= image.stat().st_size:
        raise RuntimeError('Apptainer host image has an invalid SquashFS partition')
    with image.open('rb') as stream:
        stream.seek(start)
        if stream.read(4) != b'hsqs':
            raise RuntimeError('Apptainer host image has an invalid SquashFS partition')
    # Match rootless Apptainer extraction: user xattrs, no host device nodes.
    run([extractor, '-no-progress', '-user-xattrs', '-d', str(destination),
         '-offset', str(start), '-excludes', str(image), 'dev'])
    (destination / 'dev').mkdir(exist_ok=True)


def apptainer_image(image, *, directory=None, apptainer='apptainer'):
    """Reuse an unpacked host image inside a privileged container namespace.

    Mount mediation in an outer container need not support nested FUSE image
    mounts. Apptainer's ordinary directory-image path needs neither FUSE nor a
    new user namespace. This affects the host tools image, not guest isolation.
    """
    image = Path(image).resolve()
    if not reuse_user_namespace() or not image.is_file():
        return image
    import fcntl
    import hashlib
    import json
    import shutil
    import tempfile

    def identity():
        stat = image.stat()
        return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]

    original = identity()
    key = hashlib.sha256(json.dumps(original).encode()).hexdigest()
    store = Path(directory or os.environ.get('SANDWEAVE_HOME') or image.parent) / 'host-images'
    target = store / key
    if target.is_dir():
        return target
    store.mkdir(parents=True, exist_ok=True)
    # Different worker processes preparing the same immutable SIF publish once.
    # Already prepared images take the lock-free path above.
    with (store / (key + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.is_dir():
            return target
        temporary = Path(tempfile.mkdtemp(prefix='.extract-', dir=store))
        try:
            _extract_host_image(image, temporary / 'rootfs', apptainer)
            if identity() != original:
                raise RuntimeError('Apptainer image changed while preparing it: ' + str(image))
            (temporary / 'rootfs').rename(target)
        finally:
            shutil.rmtree(temporary)
    return target


def configure_user_namespace(spec):
    """Recompute host identity mappings, including when restoring elsewhere."""
    linux = spec['linux']
    namespace = {'type': 'user'}
    if reuse_user_namespace():
        # Explicit OCI namespace paths are honored by the engine. This stays
        # inside the caller's existing container and grants no host privilege.
        namespace['path'] = '/proc/self/ns/user'
        linux.pop('uidMappings', None)
        linux.pop('gidMappings', None)
    else:
        linux['uidMappings'] = [{'containerID': 0, 'hostID': os.getuid(), 'size': 1}]
        linux['gidMappings'] = [{'containerID': 0, 'hostID': os.getgid(), 'size': 1}]
    linux['namespaces'] = [ns for ns in linux.get('namespaces', []) if ns['type'] != 'user'] + [namespace]


if __name__ == '__main__':
    # Keep argument boundaries intact when invoked by the shell launcher.
    if len(sys.argv) < 4 or '--' not in sys.argv[2:]:
        raise SystemExit('usage: host_compat.py IMAGE APPTAINER_OPTIONS... -- COMMAND...')
    boundary = sys.argv.index('--', 2)
    image = apptainer_image(sys.argv[1])
    os.execvp('apptainer', ['apptainer', 'exec', *apptainer_options(),
                          *sys.argv[2:boundary], str(image), *sys.argv[boundary + 1:]])

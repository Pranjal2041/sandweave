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
    import subprocess
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
            result = subprocess.run([apptainer, 'build', '--sandbox', str(temporary / 'rootfs'), str(image)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                env={**os.environ, 'APPTAINER_TMPDIR': str(temporary)})
            if result.returncode:
                raise RuntimeError('Preparing the Apptainer host image failed:\n' + result.stdout[-8000:])
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

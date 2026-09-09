"""Private worker state and immutable runtime inputs, independent of the old lab."""
from contextlib import contextmanager
import fcntl
import hashlib
from importlib import resources
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile

from .errors import ResourceUnavailable


def home():
    return Path(os.environ.get('SANDWEAVE_HOME', Path.home() / '.local/share/sandweave')).expanduser().resolve()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def engine_sources():
    installed = resources.files('sandweave.sandbox.runtimes.gvisor').joinpath('_engine')
    if installed.joinpath('run-gvisor.py').is_file():
        return Path(str(installed))
    # Editable development only. Built wheels include their own engine sources.
    for parent in Path(__file__).resolve().parents:
        if (parent / 'pyproject.toml').is_file() and (parent / 'scripts/run-gvisor.py').is_file():
            return parent / 'scripts'
    raise ResourceUnavailable('engine sources are missing from this installation')


def assets():
    config_file = home() / 'config.json'
    config = json.loads(config_file.read_text()) if config_file.exists() else {}
    selected = os.environ.get('SANDWEAVE_ASSETS') or config.get('assets')
    if not selected:
        # The existing lab is usable without copying a private path into the SDK.
        for parent in (Path.cwd(), *Path.cwd().parents):
            if (parent / 'images/gvisor-ubuntu-ready-ae303ca.erofs').is_file():
                selected = str(parent)
                break
    if not selected:
        raise ResourceUnavailable('runtime assets are not configured; set SANDWEAVE_ASSETS to the prepared asset directory')
    result = Path(selected).expanduser().resolve()
    if not (result / 'tools/debian-trixie.sif').is_file():
        raise ResourceUnavailable('asset directory is missing the unprivileged host image: ' + str(result))
    return result


def _immutable(source, destination):
    source, destination = Path(source), Path(destination)
    if destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def stage_tree(source, destination):
    """Idempotently stage immutable trees, including existing relative symlinks."""
    source, destination = Path(source), Path(destination)
    if source.is_symlink():
        target = os.readlink(source)
        if destination.is_symlink():
            if os.readlink(destination) != target:
                raise ValueError('staged immutable symlink changed: ' + str(destination))
        elif destination.exists():
            raise ValueError('staged immutable entry changed type: ' + str(destination))
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(target)
    elif source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            stage_tree(child, destination / child.name)
    else:
        _immutable(source, destination)


def prepare():
    source = engine_sources()
    files = sorted(p for p in source.iterdir()
                   if p.suffix in ('.py', '.sh', '.c') and not p.name.startswith('test-'))
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode()); digest.update(path.read_bytes())
    key = socket.gethostname() + '-' + os.environ.get('SLURM_JOB_ID', 'local')
    root = home() / 'workers' / key / digest.hexdigest()[:16]
    with locked(root / '.prepare.lock'):
        if (root / 'prepared.json').exists():
            return root
        base = assets()
        for directory in ('scripts', 'runs', 'downloads', 'snapshots', 'images/fixtures', 'tools/gpu'):
            (root / directory).mkdir(parents=True, exist_ok=True)
        for path in files:
            shutil.copy2(path, root / 'scripts' / path.name)
        for name in ('bench', 'seccomp-trap', 'gs-base-probe', 'debian-trixie.sif'):
            _immutable(base / 'tools' / name, root / 'tools' / name)
        for directory in ('runtime-builds', 'fast-io', 'network', 'erofs', 'gvisor-nightly-20260906'):
            src = base / 'tools' / directory
            if src.is_dir():
                stage_tree(src, root / 'tools' / directory)
        (root / 'tools/gvisor-socket').mkdir(exist_ok=True)
        shutil.copy2(base / 'tools/gvisor-socket/runtime.json', root / 'tools/gvisor-socket/runtime.json')
        revisions = base / 'notes/source-revisions.json'
        if revisions.is_file():
            info = json.loads(revisions.read_text())['gvisor']
            candidate = info.get('candidate_runtime_build')
            if candidate and (root / candidate / 'manifest.json').is_file():
                atomic_json(root / 'tools/gvisor-socket/runtime.json', {
                    'path': candidate, 'sha256': json.loads((root / candidate / 'manifest.json').read_text())})
        _immutable(base / 'images/gvisor-ubuntu-ready-ae303ca.erofs',
                   root / 'images/gvisor-ubuntu-ready-ae303ca.erofs')
        # Snapshots retain a durable path; only active runtime working data is local.
        local = Path(tempfile.mkdtemp(prefix='sandweave-' + str(os.getuid()) + '-', dir='/tmp'))
        for directory in ('gvisor/bundles', 'gvisor/state', 'gvisor/checkpoints'):
            (local / directory).mkdir(parents=True, exist_ok=True)
        (root / 'runs/local-path.txt').write_text(str(local) + '\n')
        atomic_json(root / 'prepared.json', {'assets': str(base), 'engine_sources_sha256': digest.hexdigest(),
                                            'hostname': socket.gethostname(), 'local': str(local)})
    return root

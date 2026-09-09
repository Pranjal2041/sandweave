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
import threading
import weakref

from .errors import ResourceUnavailable

_path_locks = weakref.WeakValueDictionary()
_path_locks_guard = threading.Lock()


def default_home():
    return Path.home() / '.local/share/sandweave'


def home():
    selected = os.environ.get('SANDWEAVE_HOME')
    if not selected:
        location = default_home() / 'location.json'
        if location.exists():
            selected = json.loads(location.read_text())['path']
    return Path(selected or default_home()).expanduser().resolve()


def tool_path():
    """Tools installed by setup are private to Sandweave, without shell edits."""
    directories = [str(home() / 'bin')]
    # Keep tools from the earlier default installation usable after setup
    # selects storage elsewhere. Explicit isolated homes do not inherit them.
    if not os.environ.get('SANDWEAVE_HOME') and home() != default_home().resolve():
        directories.append(str(default_home() / 'bin'))
    return os.pathsep.join([*directories, os.environ.get('PATH', '')])


def tool(name):
    return shutil.which(name, path=tool_path())


def worker_key():
    """Workers may only share authority within the same eligible resource set."""
    eligibility = {'assets': asset_identity(), 'cpus': sorted(os.sched_getaffinity(0)),
                   'gpu': {key: os.environ.get(key) for key in
                           ('SLURM_STEP_GPUS', 'SLURM_JOB_GPUS', 'CUDA_VISIBLE_DEVICES', 'NVIDIA_VISIBLE_DEVICES', 'SANDWEAVE_GPU_DEVICES')},
                   'memory': {key: os.environ.get(key) for key in
                              ('SLURM_MEM_PER_NODE', 'SLURM_MEM_PER_CPU', 'SANDWEAVE_MEMORY_BUDGET')}}
    digest = hashlib.sha256(json.dumps(eligibility, sort_keys=True).encode()).hexdigest()[:12]
    return socket.gethostname() + '-' + os.environ.get('SLURM_JOB_ID', 'local') + '-' + digest


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
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Shared filesystems can implement flock ownership per client/process.
    # Serialize our threads before entering that filesystem lock, and release
    # explicitly instead of depending on close while another fd is waiting.
    with _path_locks_guard:
        thread_lock = _path_locks.setdefault(str(path), threading.Lock())
    with thread_lock, path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def engine_sources():
    installed = resources.files('sandweave.sandbox.runtimes.gvisor').joinpath('_engine')
    if installed.joinpath('run-gvisor.py').is_file():
        return Path(str(installed))
    # Editable development only. Built wheels include their own engine sources.
    for parent in Path(__file__).resolve().parents:
        if (parent / 'pyproject.toml').is_file() and (parent / 'scripts/run-gvisor.py').is_file():
            return parent / 'scripts'
    raise ResourceUnavailable('engine sources are missing from this installation')


def assets(*, directory=None, selected=None):
    config_file = (Path(directory) if directory else home()) / 'config.json'
    config = json.loads(config_file.read_text()) if config_file.exists() else {}
    selected = selected or os.environ.get('SANDWEAVE_ASSETS') or config.get('assets')
    if not selected:
        # The existing lab is usable without copying a private path into the SDK.
        for parent in (Path.cwd(), *Path.cwd().parents):
            if (parent / 'images/gvisor-ubuntu-ready-ae303ca.erofs').is_file():
                selected = str(parent)
                break
    if not selected:
        raise ResourceUnavailable('runtime files are not configured; run sandweave setup')
    result = Path(selected).expanduser().resolve()
    if not (result / 'tools/debian-trixie.sif').is_file():
        raise ResourceUnavailable('asset directory is missing the unprivileged host image: ' + str(result))
    return result


def asset_identity(source=None):
    """Different prepared inputs get new workers; existing workers remain intact."""
    source = Path(source).resolve() if source else assets()
    digest = hashlib.sha256(str(source).encode())
    for relative in ('sandweave-assets.json', 'tools/gvisor-socket/runtime.json', 'notes/source-revisions.json'):
        path = source / relative
        digest.update(relative.encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _immutable(source, destination):
    source, destination = Path(source), Path(destination)
    size = source.stat().st_size
    if destination.is_file() and destination.stat().st_size == size:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + destination.name + '.', dir=destination.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        temporary.unlink()
        try:
            os.link(source, temporary)
        except OSError:
            # A cross-filesystem copy can fail or be interrupted. Never expose
            # its partial output at the published immutable path.
            shutil.copy2(source, temporary)
            with temporary.open('rb') as stream:
                os.fsync(stream.fileno())
        if temporary.stat().st_size != size:
            raise ResourceUnavailable('Runtime file copy is incomplete: ' + str(source))
        os.replace(temporary, destination)
    except OSError as error:
        raise ResourceUnavailable('Could not stage runtime file ' + str(source) +
                                  ' in ' + str(destination.parent) + ': ' + str(error)) from error
    finally:
        temporary.unlink(missing_ok=True)


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
        # These are deliberately guest-readable immutable assets. The worker's
        # private umask must not turn directories exposed to guest UID 1000 into
        # root-only paths. The surrounding worker workspace remains private.
        destination.chmod(0o755)
        for child in source.iterdir():
            stage_tree(child, destination / child.name)
    else:
        _immutable(source, destination)


def prepare():
    base = assets()
    source = engine_sources()
    files = sorted(p for p in source.iterdir()
                   if p.suffix in ('.py', '.sh', '.c') and not p.name.startswith(('test-', 'sdk-')))
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.name.encode()); digest.update(path.read_bytes())
    engine_digest = digest.hexdigest()
    sdk = Path(__file__).resolve().parents[1]
    sdk_digest = hashlib.sha256()
    for path in sorted(sdk.rglob('*')):
        if path.is_file() and path.suffix in ('.py', '.toml', '.sh') and '_engine' not in path.parts:
            sdk_digest.update(str(path.relative_to(sdk)).encode()); sdk_digest.update(path.read_bytes())
    digest.update(sdk_digest.digest())
    digest.update(asset_identity(base).encode())
    root = home() / 'workers' / worker_key() / digest.hexdigest()[:16]
    with locked(root / '.prepare.lock'):
        if (root / 'prepared.json').exists():
            prepared = json.loads((root / 'prepared.json').read_text())
            staged = prepared.get('staged_files', {})
            if (Path(prepared['local']).is_dir() and staged and
                    all((root / name).is_file() and (root / name).stat().st_size == size
                        for name, size in staged.items())):
                return root
            # Revalidate legacy workspaces and repair incomplete staged files.
            # Durable checkpoints also outlive node-local runtime storage.
        for directory in ('scripts', 'runs', 'downloads', 'snapshots', 'images/fixtures', 'tools/gpu'):
            (root / directory).mkdir(parents=True, exist_ok=True)
        for path in files:
            shutil.copy2(path, root / 'scripts' / path.name)
        if (base / 'sandweave-assets.json').is_file():
            shutil.copy2(base / 'sandweave-assets.json', root / 'sandweave-assets.json')
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
        prepared_path = root / 'prepared.json'
        previous = json.loads(prepared_path.read_text()) if prepared_path.exists() else {}
        local = Path(previous['local']) if previous.get('local') else None
        if local is None or not local.is_dir():
            local = Path(tempfile.mkdtemp(prefix='sandweave-' + str(os.getuid()) + '-', dir='/tmp'))
        for directory in ('gvisor/bundles', 'gvisor/state', 'gvisor/checkpoints'):
            (local / directory).mkdir(parents=True, exist_ok=True)
        (root / 'runs/local-path.txt').write_text(str(local) + '\n')
        registry = base / 'sandweave-assets.json'
        staged = {str(path.relative_to(root)): path.stat().st_size
                  for directory in ('tools', 'images') for path in (root / directory).rglob('*')
                  if path.is_file()}
        atomic_json(root / 'prepared.json', {'assets': str(base), 'engine_sources_sha256': engine_digest,
                                            'sdk_sources_sha256': sdk_digest.hexdigest(),
                                            'assets_sha256': hashlib.sha256(registry.read_bytes()).hexdigest() if registry.exists() else None,
                                            'staged_files': staged,
                                            'hostname': socket.gethostname(), 'local': str(local)})
    return root

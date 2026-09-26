"""Select and install a pinned runtime release before falling back to a build."""
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sys
import tempfile
import urllib.error
import uuid

from .sandbox import workspace
from .setup_progress import Stage

PIN = Path(__file__).with_name('runtime-release.json')
MINIMUM_KERNEL = (5, 4, 0)

def kernel_version(value):
    match = re.match(r'^(\d+)\.(\d+)(?:\.(\d+))?', value)
    if not match:
        raise ValueError('Cannot determine Linux kernel version: ' + value)
    return tuple(int(part or 0) for part in match.groups())


def check_platform():
    """Check runtime host requirements without installing or probing containers."""
    architecture = {'amd64': 'x86_64'}.get(platform.machine(), platform.machine())
    if platform.system() != 'Linux' or architecture != 'x86_64':
        raise ValueError('Sandweave currently supports Linux x86-64 workers; this architecture needs a port, not a local rebuild')
    release = platform.release()
    kernel = kernel_version(release)
    if kernel < MINIMUM_KERNEL:
        raise ValueError('Sandweave\'s gVisor runtime requires Linux 5.4 or newer '
                         '(found ' + release + '). Use a worker with a supported kernel; '
                         'reinstalling or compiling Sandweave does not upgrade the host kernel.')
    return {'architecture': architecture, 'kernel': kernel}


def host_info():
    info = check_platform()
    flags = []
    try:
        eligible = os.sched_getaffinity(0)
        for block in Path('/proc/cpuinfo').read_text().split('\n\n'):
            fields = dict(line.split(':', 1) for line in block.splitlines() if ':' in line)
            fields = {key.strip(): value.strip() for key, value in fields.items()}
            if int(fields.get('processor', '-1')) in eligible:
                flags.append(set(fields.get('flags', '').split()))
    except (OSError, ValueError):
        flags = []
    return {**info, 'cpu_flags': sorted(set.intersection(*flags)) if flags else []}


def runtime_supported(root):
    """Legacy prepared runtimes predate Linux 5.4 support."""
    descriptor = json.loads((Path(root) / 'tools/gvisor-socket/runtime.json').read_text())
    minimum = descriptor.get('minimum_kernel', '5.6')
    revisions = Path(root) / 'notes/source-revisions.json'
    if revisions.is_file():
        candidate = json.loads(revisions.read_text()).get('gvisor', {}).get('candidate_runtime_build')
        if candidate and candidate != descriptor['path'] and (Path(root) / candidate / 'manifest.json').is_file():
            minimum = '5.6'
    return check_platform()['kernel'] >= kernel_version(minimum)


def runtime_current(root):
    """Upgrade prepared engines for new launches; live snapshots stay pinned."""
    descriptor = json.loads((Path(root) / 'tools/gvisor-socket/runtime.json').read_text())
    return (runtime_supported(root) and descriptor.get('cpu_accounting', 0) >= 1
            and descriptor.get('bridge_local_delivery', 0) >= 1
            and descriptor.get('disk_storage', 0) >= 1)


SECCOMP_PROBE = '''import ctypes, os
class Filter(ctypes.Structure):
    _fields_ = [('code',ctypes.c_ushort),('jt',ctypes.c_ubyte),('jf',ctypes.c_ubyte),('k',ctypes.c_uint)]
class Program(ctypes.Structure):
    _fields_ = [('len',ctypes.c_ushort),('filter',ctypes.POINTER(Filter))]
instruction=Filter(6,0,0,0x7fff0000)
program=Program(1,ctypes.pointer(instruction))
libc=ctypes.CDLL(None,use_errno=True)
if libc.prctl(38,1,0,0,0) or libc.prctl(22,2,ctypes.byref(program),0,0):
    raise OSError(ctypes.get_errno(),os.strerror(ctypes.get_errno()))
print('NAMESPACE_AND_SECCOMP_OK')
'''


def check_host(directory):
    """Probe Apptainer's actual permission path without downloading a rootfs."""
    from .onboarding import run_probe
    info = host_info()
    apptainer = workspace.tool('apptainer')
    if not apptainer:
        raise ValueError('Apptainer is required before checking runtime compatibility')
    with Stage('Check runtime compatibility'), tempfile.TemporaryDirectory(
            prefix='.host-check-', dir=directory) as temporary:
        root = Path(temporary)
        for name in ('bin', 'usr', 'lib', 'lib64', 'etc', 'tmp', 'var/tmp', 'home', 'dev', 'proc', 'sys'):
            (root / name).mkdir(parents=True, exist_ok=True)
        for name in ('passwd', 'group'):
            (root / 'etc' / name).touch()
        command = [apptainer, 'exec', *workspace.host_compat().apptainer_options(no_mount=('tmp', 'home', 'cwd')),
                   '--contain', '--ipc', '--cleanenv', '--no-home']
        # Use only the host's trusted interpreter and libraries for this probe.
        # -I -S prevents importing user packages, sitecustomize or PYTHONPATH.
        base_prefix = str(Path(sys.base_prefix).resolve())
        for name in dict.fromkeys(('/usr', '/bin', '/lib', '/lib64', base_prefix)):
            if Path(name).exists():
                (root / name.lstrip('/')).mkdir(parents=True, exist_ok=True)
                command += ['--bind', name + ':' + name + ':ro']
        interpreter = str(Path(sys.executable).resolve())
        ok, detail = run_probe([*command, temporary, interpreter, '-I', '-S', '-c', SECCOMP_PROBE], timeout=30)
        if not ok or 'NAMESPACE_AND_SECCOMP_OK' not in detail:
            raise ValueError('The runtime host check failed before downloading or building the engine. '
                             'Check the container and seccomp error below; rebuilding does not bypass host restrictions.\n' + detail)
    return info


def release_manifest(directory):
    from .bootstrap import download
    pin = json.loads(PIN.read_text())
    if not pin.get('manifest'):
        return None
    entry = pin['manifest']
    path = download(entry['url'], Path(directory) / 'downloads',
                    'runtime-manifest-' + entry['sha256'] + '.json', sha256=entry['sha256'])
    if path.stat().st_size > 128 * 1024:
        raise ValueError('Runtime manifest is too large')
    manifest = json.loads(path.read_text())
    if not isinstance(manifest, dict) or manifest.get('schema_version') != 1 or not isinstance(manifest.get('artifacts'), list):
        raise ValueError('Invalid runtime release manifest')
    return manifest


def select(manifest, host):
    from .bootstrap import build_input
    if manifest.get('engine_patch_sha256') != workspace.file_digest(build_input('gvisor-no-kvm-prototype.patch')):
        return None, 'the release does not match this SDK\'s engine source'
    for item in manifest['artifacts']:
        if item['architecture'] != host['architecture']:
            continue
        if host['kernel'] < kernel_version(item['minimum_kernel']):
            continue
        if not set(item['cpu_flags']) <= set(host['cpu_flags']):
            continue
        if not re.fullmatch(r'[a-zA-Z0-9._-]+\.tar\.gz', item['name']):
            raise ValueError('Invalid runtime archive filename')
        if not re.fullmatch(r'[0-9a-f]{64}', item['sha256']):
            raise ValueError('Invalid runtime archive checksum')
        if not isinstance(item['size'], int) or item['size'] <= 0 or not isinstance(item['unpacked_bytes'], int) or item['unpacked_bytes'] <= 0:
            raise ValueError('Invalid runtime archive sizes')
        return item, None
    return None, 'no published binary matches this machine\'s requirements'


def install(directory, host):
    """Return verified engine files, or None when a source build is needed."""
    from .bootstrap import download, extract_source
    try:
        manifest = release_manifest(directory)
    except (urllib.error.URLError, TimeoutError) as error:
        print('Runtime release unavailable; building from source. ' + str(error), file=sys.stderr, flush=True)
        return None
    if manifest is None:
        return None
    artifact, reason = select(manifest, host)
    if artifact is None:
        print('Building from source: ' + reason + '.', file=sys.stderr, flush=True)
        return None
    store = Path(directory) / 'assets'
    store.mkdir(parents=True, exist_ok=True)
    target = store / ('release-' + artifact['sha256'][:24])
    if target.exists():
        try:
            validate_engine(target)
            return target
        except (OSError, ValueError, KeyError, TypeError):
            # Another worker may still use this directory. Never repair in place.
            target = target.with_name(target.name + '-' + uuid.uuid4().hex[:12])
    print('Using prebuilt runtime ' + manifest['version'] + ' for ' + host['architecture'] + '.',
          file=sys.stderr, flush=True)
    try:
        archive = download(artifact['url'], Path(directory) / 'downloads', artifact['name'], sha256=artifact['sha256'])
    except (urllib.error.URLError, TimeoutError) as error:
        print('Runtime download unavailable; building from source. ' + str(error), file=sys.stderr, flush=True)
        return None
    if archive.stat().st_size != artifact['size']:
        raise ValueError('Runtime archive size differs from its manifest')
    temporary = Path(tempfile.mkdtemp(prefix='.release-', dir=store))
    try:
        extract_source(archive, temporary, max_bytes=artifact['unpacked_bytes'])
        with Stage('Verify runtime files'):
            validate_engine(temporary)
        temporary.rename(target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return target


def validate_engine(root):
    from .installation import validate_installation
    from .onboarding import _inside
    root = Path(root).resolve()
    validate_installation(root)
    descriptor = json.loads((root / 'tools/gvisor-socket/runtime.json').read_text())
    build = _inside(root, descriptor['path'])
    if not build.is_relative_to(root / 'tools/runtime-builds'):
        raise ValueError('Runtime build escapes its immutable store')
    hashes = descriptor['sha256']
    required = {'runsc', 'gvisor-bin/gvisor_sentry', 'gvisor-bin/checkpointgofer',
                'gvisor-bin/gvisor-sentry-prewarmer', 'gvisor-bin/runsc-metric-server'}
    if not required <= hashes.keys() or json.loads((build / 'manifest.json').read_text()) != hashes:
        raise ValueError('Incomplete runtime engine manifest')
    for name, expected in hashes.items():
        if workspace.file_digest(_inside(build, name)) != expected:
            raise ValueError('Runtime engine checksum mismatch: ' + name)
        if name in required and not os.access(_inside(build, name), os.X_OK):
            raise ValueError('Runtime engine file is not executable: ' + name)
    return descriptor

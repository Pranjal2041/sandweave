"""Build runtime inputs from upstream sources in private, unprivileged containers."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import uuid

from .sandbox import workspace
from .installation import record_installation
from .setup_progress import Stage, run_logged

BUILDER = ('docker://us-central1-docker.pkg.dev/gvisor-presubmit/'
           'gvisor-presubmit-images/default_x86_64:c48008cead6d6826')
GVISOR_BASE = '0a1316b0d180600212bd607aa0ccfe2a9b09a899'
UBUNTU_URL = ('https://cdimage.ubuntu.com/ubuntu-base/releases/22.04/release/'
              'ubuntu-base-22.04.5-base-amd64.tar.gz')
UBUNTU_SHA256 = '242cd8898b33ea806ef5f13b1076ed7c76f9f989d18384452f7166692438ff1a'
IMAGE = 'images/gvisor-ubuntu-ready-ae303ca.erofs'


def build_input(name):
    engine = workspace.engine_sources()
    for path in (engine / name, engine.parent / 'notes' / name, engine.parent / 'sources' / name):
        if path.is_file():
            return path
    raise ValueError('The installed package is missing build input ' + name)


def download(url, directory, name, *, sha256=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    receipt = directory / (name + '.download.json')
    if target.is_file():
        expected = sha256
        if expected is None and receipt.is_file():
            try:
                saved = json.loads(receipt.read_text())
            except (OSError, ValueError):
                saved = {}
            if not isinstance(saved, dict):
                saved = {}
            if saved.get('url') == url:
                expected = saved.get('sha256')
        if expected and workspace.file_digest(target) == expected:
            return target
    fd, filename = tempfile.mkstemp(prefix='.' + name + '.', dir=directory)
    temporary = Path(filename)
    try:
        with os.fdopen(fd, 'wb') as output, Stage('Download ' + name, unit='bytes', detail='Connecting') as progress:
            with urllib.request.urlopen(url, timeout=60) as response:
                length = response.headers.get('Content-Length', '')
                total = int(length) if length.isdecimal() else None
                progress.update(total=total, detail='Downloading')
                read = getattr(response, 'read1', response.read)
                while chunk := read(256 * 1024):
                    output.write(chunk)
                    progress.update(advance=len(chunk))
                if total is not None and output.tell() != total:
                    raise ValueError('Incomplete download: ' + name)
            progress.update(detail='Verifying download')
            output.flush()
            os.fsync(output.fileno())
            actual = workspace.file_digest(temporary)
            if sha256 is not None and actual != sha256:
                raise ValueError('Download checksum mismatch: ' + name)
            os.replace(temporary, target)
            workspace.atomic_json(receipt, {'url': url, 'sha256': actual, 'size': target.stat().st_size})
        return target
    finally:
        temporary.unlink(missing_ok=True)


def extract_source(archive, destination, *, max_bytes=None):
    """Extract source trees without device files, host ownership or escaping links."""
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with Stage('Extract ' + Path(archive).name, unit='files', detail='Reading archive') as progress, tarfile.open(archive) as source:
        members = source.getmembers()
        if max_bytes is not None and sum(member.size for member in members) > max_bytes:
            raise ValueError('Runtime archive exceeds its declared unpacked size')
        progress.update(total=len(members))
        top = {Path(member.name).parts[0] for member in members if Path(member.name).parts}
        if len(top) != 1:
            raise ValueError('Source archive must have a single top-level directory')
        prefix = next(iter(top))
        for member in members:
            progress.update(detail=member.name)
            relative = Path(member.name)
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('Source archive path escapes its directory')
            relative = relative.relative_to(prefix)
            if relative == Path('.'):
                progress.update(advance=1)
                continue
            path = destination / relative
            if not path.resolve().is_relative_to(destination):
                raise ValueError('Source archive traverses an escaping link')
            path.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                path.mkdir(exist_ok=True)
            elif member.isfile():
                if path.is_symlink():
                    raise ValueError('Source archive replaces a symlink with a file')
                with source.extractfile(member) as stream, path.open('wb') as output:
                    shutil.copyfileobj(stream, output)
                path.chmod(member.mode & 0o777)
            elif member.issym():
                target = Path(member.linkname)
                if target.is_absolute() or not (path.parent / target).resolve().is_relative_to(destination):
                    raise ValueError('Source archive contains an escaping link')
                path.symlink_to(member.linkname)
            else:
                raise ValueError('Unsupported source archive entry: ' + member.name)
            progress.update(advance=1)


class Builder:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.downloads = self.directory / 'downloads'
        self.downloads.mkdir(parents=True, exist_ok=True)
        self.apptainer = workspace.tool('apptainer')
        if not self.apptainer:
            raise ValueError('Install Apptainer before preparing runtime files')
        self.env = {**os.environ, 'PATH': workspace.tool_path(), 'SANDWEAVE_PYTHON': sys.executable,
                    'APPTAINER_CACHEDIR': str(self.downloads / 'apptainer-cache'),
                    'APPTAINER_TMPDIR': str(self.directory / 'build/apptainer-tmp'),
                    'TMPDIR': str(self.directory / 'build/tmp')}
        for name in ('APPTAINER_CACHEDIR', 'APPTAINER_TMPDIR', 'TMPDIR'):
            Path(self.env[name]).mkdir(parents=True, exist_ok=True)

    def run(self, command, *, cwd=None, label='build'):
        return run_logged(command, self.directory, label=label, env=self.env, cwd=cwd)

    def pull(self, reference, target):
        target = Path(target)
        receipt = target.with_suffix('.image.json')
        if target.is_file() and receipt.is_file():
            try:
                info = json.loads(receipt.read_text())
            except (OSError, ValueError):
                info = {}
            if not isinstance(info, dict):
                info = {}
            if info.get('reference') == reference and workspace.file_digest(target) == info.get('sha256'):
                return
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name('.' + target.name + '-' + uuid.uuid4().hex)
        try:
            self.run([self.apptainer, 'pull', str(temporary), reference], label='Download container image')
            with Stage('Verify ' + target.name):
                actual = workspace.file_digest(temporary)
                os.replace(temporary, target)
                workspace.atomic_json(receipt, {'reference': reference, 'sha256': actual})
        finally:
            temporary.unlink(missing_ok=True)

    def container(self, root, image, *command):
        (root / 'build-tmp/home').mkdir(parents=True, exist_ok=True)
        return [self.apptainer, 'exec', '--userns', '--containall', '--cleanenv', '--no-home',
                '--bind', str(root) + ':/lab', '--bind', str(root / 'build-tmp') + ':/tmp',
                '--env', 'HOME=/tmp/home', '--env', 'TMPDIR=/tmp',
                '--env', 'GOCACHE=/tmp/go-cache', '--env', 'BAZELISK_HOME=/tmp/bazelisk',
                '--env', 'XDG_CACHE_HOME=/tmp/cache',
                '--pwd', '/lab', str(image), *command]

    def helpers(self, root):
        # apt downloads and dpkg extraction run as the caller in a writable bind.
        # The image supplies the matching libc for these Debian helper binaries.
        script = '''set -eu
mkdir -p /lab/build-tmp/prepared-helpers /lab/build-tmp/apt/lists/partial /lab/build-tmp/apt/cache/archives/partial
cd /lab/build-tmp/apt
set -- -o Dir::State::lists=/lab/build-tmp/apt/lists -o Dir::Cache=/lab/build-tmp/apt/cache -o APT::Sandbox::User=root -o Debug::NoLocking=true
apt-get "$@" update
apt-get "$@" --download-only --reinstall --no-install-recommends -y install erofs-utils passt iproute2 util-linux x11proto-dev
for package in cache/archives/*.deb; do dpkg-deb -x "$package" /lab/build-tmp/prepared-helpers; done
'''
        self.run(self.container(root, root / 'tools/debian-trixie.sif', 'sh', '-c', script), label='Prepare runtime utilities')
        # The caller owns this unpublished build/import. Its previous files
        # may be hardlinks into another installation, so replace the directory.
        if (root / 'tools/helpers').exists():
            shutil.rmtree(root / 'tools/helpers')
        (root / 'build-tmp/prepared-helpers').rename(root / 'tools/helpers')

    def engine(self, root):
        archive = download('https://codeload.github.com/google/gvisor/tar.gz/' + GVISOR_BASE,
                           self.downloads, 'gvisor-' + GVISOR_BASE + '.tar.gz')
        extract_source(archive, root / 'sources/gvisor')
        shutil.copy2(build_input('gvisor-no-kvm-prototype.patch'), root / 'engine.patch')
        cpus = max(1, min(8, len(os.sched_getaffinity(0))))
        script = '''set -eu
cd /lab/sources/gvisor
git apply /lab/engine.patch
bazel --batch --output_user_root=/lab/build-tmp/bazel build --jobs="$1" \\
  //runsc:runsc //runsc/cmd/sentry:gvisor_sentry \\
  //runsc/checkpointgofer:checkpointgofer_binary //runsc/prewarmer:gvisor-sentry-prewarmer \\
  //runsc/cmd/metricserver:runsc-metric-server
mkdir -p /lab/artifacts/gvisor-bin
cp LICENSE /lab/artifacts/LICENSE
cp bazel-bin/runsc/runsc_/runsc /lab/artifacts/runsc
cp bazel-bin/runsc/cmd/sentry/gvisor_sentry_/gvisor_sentry /lab/artifacts/gvisor-bin/gvisor_sentry
cp bazel-bin/runsc/checkpointgofer/checkpointgofer_binary_/checkpointgofer_binary /lab/artifacts/gvisor-bin/checkpointgofer
cp bazel-bin/runsc/prewarmer/gvisor-sentry-prewarmer /lab/artifacts/gvisor-bin/gvisor-sentry-prewarmer
cp bazel-bin/runsc/cmd/metricserver/runsc-metric-server_/runsc-metric-server /lab/artifacts/gvisor-bin/runsc-metric-server
for name in bench seccomp-trap gs-base-probe; do
  cc -O2 -static "/lab/input/$name.c" -o "/lab/tools/$name" -pthread
done
'''
        self.run(self.container(root, root / 'tools/gvisor-builder.sif', 'sh', '-c', script, 'build', str(cpus)),
                 label='Build gVisor')
        artifacts = root / 'artifacts'
        hashes = {str(path.relative_to(artifacts)): workspace.file_digest(path)
                  for path in artifacts.rglob('*') if path.is_file()}
        identity = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        relative = 'tools/runtime-builds/' + identity
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        artifacts.rename(root / relative)
        workspace.atomic_json(root / relative / 'manifest.json', hashes)
        workspace.atomic_json(root / 'tools/gvisor-socket/runtime.json', {'path': relative, 'sha256': hashes})

    def erofs(self, root, source, output):
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name('.' + output.name + '-' + uuid.uuid4().hex)
        command = ['env', 'LD_LIBRARY_PATH=/lab/tools/helpers/usr/lib/x86_64-linux-gnu',
                   '/lab/tools/helpers/usr/bin/mkfs.erofs', '--tar=f', '-E', 'noinline_data',
                   '/lab/' + str(temporary.relative_to(root)), '/lab/' + str(source.relative_to(root))]
        try:
            self.run(self.container(root, root / 'tools/debian-trixie.sif', *command), label='Create guest image')
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)

    def build(self, template, recipe, *, base=None, engine=None):
        store = self.directory / 'assets'
        store.mkdir(parents=True, exist_ok=True)
        # Only this attempt owns these paths; no running worker points into them.
        root = Path(tempfile.mkdtemp(prefix='.build-', dir=store))
        try:
            return self._build(root, recipe, base=base, engine=engine)
        except BaseException:
            # Preserve the exact failed workspace and logs for diagnosis. The
            # setup lock prevents another attempt from mistaking it for ready.
            print('Build stopped. Working files: ' + str(root), file=sys.stderr, flush=True)
            raise

    def _build(self, root, recipe, *, base=None, engine=None):
        from .onboarding import validate_assets
        for name in ('tools', 'scripts', 'input', 'build-tmp', 'images/fixtures', 'runs', 'snapshots'):
            (root / name).mkdir(parents=True, exist_ok=True)
        for source in workspace.engine_files():
            shutil.copy2(source, root / 'scripts' / source.name)
            if source.suffix == '.sh':
                (root / 'scripts' / source.name).chmod(0o755)
        for name in ('bench.c', 'seccomp-trap.c', 'gs-base-probe.c'):
            shutil.copy2(build_input(name), root / 'input' / name)
        if base is not None:
            # `base` is a managed installation, never an arbitrary lab directory.
            # Keep already installed templates when adding another workload.
            for name in ('tools', 'images', 'snapshots'):
                if (Path(base) / name).is_dir():
                    workspace.stage_tree(Path(base) / name, root / name)
            previous = json.loads((Path(base) / 'sandweave-assets.json').read_text())
        else:
            previous = {}
            self.pull('docker://debian:trixie-slim', self.downloads / 'debian-trixie.sif')
            self.run(self.container(root, self.downloads / 'debian-trixie.sif', '/bin/true'),
                     label='Check container permissions')
            workspace._immutable(self.downloads / 'debian-trixie.sif', root / 'tools/debian-trixie.sif')
            if engine is None:
                self.pull(BUILDER, self.downloads / 'gvisor-builder.sif')
                workspace._immutable(self.downloads / 'gvisor-builder.sif', root / 'tools/gvisor-builder.sif')
                self.engine(root)
            else:
                from .releases import validate_engine
                descriptor = validate_engine(engine)
                workspace.stage_tree(Path(engine) / descriptor['path'], root / descriptor['path'])
                workspace.atomic_json(root / 'tools/gvisor-socket/runtime.json', descriptor)
        if not (root / 'tools/gpu/bin/cuda-checkpoint').is_file():
            revision = '00d5cce84c628088d6caa203fc4af40c1538b6f7'
            checkpoint = download('https://raw.githubusercontent.com/NVIDIA/cuda-checkpoint/' + revision +
                                  '/bin/x86_64_Linux/cuda-checkpoint', self.downloads, 'cuda-checkpoint',
                                  sha256='707fa7f54136824d6c1d6dd724b9b1717610f831033c00d06da474de363a06db')
            checkpoint.chmod(0o755)
            workspace._immutable(checkpoint, root / 'tools/gpu/bin/cuda-checkpoint')
        if (not (root / 'tools/helpers/usr/bin/mkfs.erofs').is_file()
                or ('desktop' in recipe['capabilities'] and not all(
                    (root / 'tools/helpers/usr/include/X11' / name).is_file()
                    for name in ('keysymdef.h', 'XF86keysym.h')))):
            self.helpers(root)
        if base is None:
            ubuntu = download(UBUNTU_URL, self.downloads, 'ubuntu-base-22.04.5-amd64.tar.gz', sha256=UBUNTU_SHA256)
            import gzip
            with Stage('Extract Ubuntu filesystem'), gzip.open(ubuntu, 'rb') as source, (root / 'ubuntu.tar').open('wb') as output:
                shutil.copyfileobj(source, output)
            self.erofs(root, root / 'ubuntu.tar', root / IMAGE)
        from .onboarding import workload
        profile = workload(recipe)
        if 'desktop' in recipe['capabilities']:
            shutil.copy2(build_input('xvnc-fast-io.c'), root / 'input/xvnc-fast-io.c')
        if profile.startswith(('vr/', 'games/')):
            self.vr_inputs(root, profile)
        mounts = [{'source': str(root / 'input'), 'destination': '/sandweave-input', 'read_only': True}]
        workspace.atomic_json(root / 'mounts.json', mounts)
        # Socket paths stay short. Large downloads, compiler output and rootfs
        # exports are all in selected storage, not this node-local directory.
        with tempfile.TemporaryDirectory(prefix='sw-build-') as active:
            (root / 'runs/local-path.txt').write_text(active + '\n')
            command = [sys.executable, str(root / 'scripts/run-gvisor.py'), '--guest-gs',
                       '--base-image', previous.get('default_image', IMAGE),
                       '--cpu-policy', 'shared', '--guest-cpus', str(max(1, min(8, len(os.sched_getaffinity(0))))),
                       '--memory-mib', '16384' if profile.startswith(('vr/', 'games/')) else '8192',
                       '--runtime-memory-mib', '1024', '--no-runtime-debug', '--mounts', str(root / 'mounts.json'),
                       '--build-output', str(root / 'output'),
                       'install-' + uuid.uuid4().hex[:12], '--', '/bin/bash',
                       '/usr/local/bin/engine-install', profile]
            # The fixture builder includes gvisor-guest-*.sh automatically.
            shutil.copy2(build_input('install-sandbox-base.sh'), root / 'scripts/gvisor-guest-install.sh')
            self.run(command, label='Install guest software')
        self.erofs(root, root / 'output/rootfs.tar', root / 'images/new.erofs')
        checksum = workspace.file_digest(root / 'images/new.erofs')
        image = 'images/ubuntu-' + checksum + '.erofs'
        (root / 'images/new.erofs').rename(root / image)
        if (root / 'output/fast-io').is_dir():
            workspace.stage_tree(root / 'output/fast-io', root / 'tools/fast-io')
        if (root / 'output/gpu').is_dir():
            workspace.stage_tree(root / 'output/gpu', root / 'tools/gpu')
        registry = {**previous, 'schema_version': 1, 'default_image': image,
                    'images': {**previous.get('images', {}), image: {
                        'size': (root / image).stat().st_size, 'sha256': checksum}}, 'snapshots': {}}
        registry['snapshots'] = previous.get('snapshots', {})
        workloads = [*previous.get('workloads', []), 'coding', 'cuda', profile]
        if 'desktop' in recipe['capabilities']:
            workloads.append('gnome')
        if profile.startswith(('vr/', 'games/')):
            workloads.append('vr/opensaber')
        if 'gunspinning' in profile:
            workloads += ['vr/gunspinning', 'games/gunspinning-gamepad']
        registry['workloads'] = sorted(set(workloads))
        if recipe.get('base_snapshot'):
            registry['snapshots'][recipe['base_snapshot']] = {'kind': 'image', 'path': image}
        if base is None:
            (root / IMAGE).unlink()
        workspace.atomic_json(root / 'sandweave-assets.json', registry)
        with Stage('Verify runtime files'):
            validate_assets(root, recipe)
        # Build work is disposable and never part of a published runtime.
        with Stage('Remove temporary build files'):
            for name in ('sources', 'build-tmp', 'input', 'output', 'scripts', 'runs'):
                shutil.rmtree(root / name, ignore_errors=True)
            for name in ('ubuntu.tar', 'engine.patch', 'mounts.json'):
                (root / name).unlink(missing_ok=True)
        with Stage('Record installed files'):
            record_installation(root)
        target = root.parent / ('built-' + uuid.uuid4().hex[:24])
        root.rename(target)
        return target

    def vr_inputs(self, root, profile):
        from .vr_installation import prepare_inputs
        prepare_inputs(self, root, profile)

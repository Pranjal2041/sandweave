"""Local installation checks and user-selected repairs for setup and doctor."""
from dataclasses import asdict, dataclass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shlex
import subprocess
import sys
import tempfile
import urllib.request
import uuid

from .sandbox import workspace
from .templates.resolve import Template
from .setup_progress import Stage, run_logged

APPTAINER_VERSION = '1.5.3'
APPTAINER_INSTALLER = ('https://raw.githubusercontent.com/apptainer/apptainer/'
                      'v1.5.3/tools/install-unprivileged.sh')
APPTAINER_INSTALLER_SHA256 = 'a097956eafa6ab3dd843ee429d0b774ed029753bfadf013b8046556228fac6f2'
PROFILES = {'coding': 'Code', 'gnome': 'Desktop', 'cuda': 'CUDA', 'docker': 'Docker',
            'vr/gunspinning': 'VR: GunSpinning', 'vr/opensaber': 'VR: Open Saber',
            'games/gunspinning-gamepad': 'GunSpinning: gamepad'}


def workload(recipe):
    installed = recipe.get('installation')
    if installed is not None:
        if installed not in PROFILES:
            raise ValueError('Unknown installation template: ' + str(installed))
        return installed
    name = recipe['name'].removeprefix('builtin:').removesuffix('@1')
    if name in PROFILES:
        return name
    if 'vr' in recipe['capabilities']:
        return 'vr/opensaber'
    if 'desktop' in recipe['capabilities']:
        return 'gnome'
    if recipe['runtime_options'].get('init') == 'docker':
        return 'docker'
    return 'coding'


@dataclass
class Check:
    name: str
    title: str
    status: str
    detail: str
    fix: str | None = None


def configuration(directory=None):
    path = (Path(directory) if directory is not None else workspace.home()) / 'config.json'
    value = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(value, dict):
        raise ValueError('Sandweave config.json must contain an object')
    return value


def save_configuration(**changes):
    path = workspace.home() / 'config.json'
    with workspace.locked(path.with_suffix('.lock')):
        workspace.atomic_json(path, {**configuration(), **changes})


def save_runtime_location(root):
    """Compatibility helper for explicitly selecting a prepared source."""
    save_configuration(assets=str(Path(root).expanduser().resolve()))
    print('Sandweave data: ' + str(workspace.home()))


def run_probe(command, *, timeout=20):
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=timeout,
                                env={**os.environ, 'PATH': workspace.tool_path()})
        detail = (result.stdout + result.stderr).strip()[-2000:]
        if result.returncode and not detail:
            detail = 'Process exited with status ' + str(result.returncode)
        return result.returncode == 0, detail
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)


def asset_candidates():
    """Search known locations only; do not crawl a user's files or their cluster."""
    candidates = [workspace.home() / 'assets']
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        candidates.extend((start, *start.parents))
    result = []
    for path in candidates:
        path = path.resolve()
        if path not in result and (path / 'tools/debian-trixie.sif').is_file():
            result.append(path)
    return result


def _inside(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('runtime file escapes its directory: ' + str(relative))
    return path


def validate_assets(root, recipe, *, contents=True):
    """Check template inputs; installation also verifies all file contents."""
    root = Path(root).expanduser().resolve()
    registry_path = root / 'sandweave-assets.json'
    registry = json.loads(registry_path.read_text()) if registry_path.exists() else {}
    if not isinstance(registry, dict):
        raise ValueError('Runtime registry must contain an object')
    if 'workloads' in registry and workload(recipe) not in registry['workloads']:
        raise ValueError('This runtime does not yet include ' + workload(recipe) + '; run sandweave setup to add it')
    default_image = registry.get('default_image', 'images/gvisor-ubuntu-ready-ae303ca.erofs')
    required = ['tools/debian-trixie.sif', 'tools/gvisor-socket/runtime.json',
                default_image]
    missing = [name for name in required if not _inside(root, name).is_file()]
    if missing:
        raise ValueError('Missing runtime files: ' + ', '.join(missing))
    descriptor = json.loads((root / 'tools/gvisor-socket/runtime.json').read_text())
    revisions = root / 'notes/source-revisions.json'
    if revisions.is_file():
        candidate = json.loads(revisions.read_text()).get('gvisor', {}).get('candidate_runtime_build')
        if candidate and (_inside(root, candidate) / 'manifest.json').is_file():
            descriptor = {'path': candidate, 'sha256': json.loads(
                (_inside(root, candidate) / 'manifest.json').read_text())}
    build = _inside(root, descriptor['path'])
    if not build.is_relative_to((root / 'tools/runtime-builds').resolve()):
        raise ValueError('runtime build is outside the immutable build store')
    hashes = descriptor['sha256']
    if not hashes or json.loads((build / 'manifest.json').read_text()) != hashes:
        raise ValueError('runtime manifest differs from the selected build')
    for name in ('runsc', 'gvisor-bin/gvisor_sentry', 'gvisor-bin/checkpointgofer',
                 'gvisor-bin/gvisor-sentry-prewarmer', 'gvisor-bin/runsc-metric-server'):
        if name not in hashes:
            raise ValueError('runtime manifest is missing ' + name)
    for name, expected in hashes.items():
        path = _inside(build, name)
        if not path.is_file():
            raise ValueError('Missing runtime file: ' + name)
        if contents and workspace.file_digest(path) != expected:
            raise ValueError('runtime checksum mismatch: ' + name)
    for name, info in registry.get('images', {}).items():
        if _inside(root, name).stat().st_size != info['size']:
            raise ValueError('image size differs from its manifest: ' + name)
        if contents and workspace.file_digest(_inside(root, name)) != info['sha256']:
            raise ValueError('image checksum mismatch: ' + name)
    if recipe.get('base_snapshot'):
        info = registry.get('snapshots', {}).get(recipe['base_snapshot'])
        if not info:
            raise ValueError('Missing prepared game base: ' + recipe['base_snapshot'])
        if info.get('kind') == 'image':
            if info['path'] not in registry.get('images', {}):
                raise ValueError('template image is absent from the image manifest')
            snapshot = None
        else:
            snapshot = _inside(root, info['path'])
    else:
        snapshot = None
    if snapshot is not None:
        manifest = json.loads((snapshot / 'snapshot-manifest.json').read_text())
        if manifest['snapshot_id'] != info['snapshot_id']:
            raise ValueError('game base differs from its registered snapshot')
        if manifest.get('kind') != 'filesystem' or 'rootfs-upper.tar' not in manifest.get('files', {}):
            raise ValueError('game base must contain a filesystem snapshot')
        for name, item in manifest['files'].items():
            if _inside(snapshot, name).stat().st_size != item['size']:
                raise ValueError('game base payload size mismatch: ' + name)
            if contents and workspace.file_digest(_inside(snapshot, name)) != item['sha256']:
                raise ValueError('game base payload checksum mismatch: ' + name)
        base = manifest['base_image']
        if _inside(root, base['path']).stat().st_size != base['size']:
            raise ValueError('game base image size mismatch')
        if contents and workspace.file_digest(_inside(root, base['path'])) != base['sha256']:
            raise ValueError('game base image checksum mismatch')
        dependency = manifest['runtime']
        for name, expected in dependency['sha256'].items():
            path = _inside(_inside(root, dependency['path']), name)
            if not path.is_file():
                raise ValueError('Missing game base runtime file: ' + name)
            if contents and workspace.file_digest(path) != expected:
                raise ValueError('game base runtime checksum mismatch: ' + name)
    additional = []
    if 'desktop' in recipe['capabilities']:
        additional += ['tools/fast-io/bridge', 'tools/fast-io/libxcb-xtest.so.0']
    if workload(recipe).startswith(('vr/', 'games/')):
        additional += ['tools/gpu/virtualgl/opt/VirtualGL/bin/vglrun',
                       'tools/gpu/compat/libvisualorder.so']
    if 'gunspinning' in workload(recipe):
        additional += ['tools/gpu/vr/gunspinning-linux-2.0.1/GunSpinningVR',
                       'tools/gpu/vr/gunspinning-linux-2.0.1/GunSpinningVR_Data/globalgamemanagers',
                       'tools/gpu/vr/xrizer-v0.5/bin/linux64/vrclient.so',
                       'tools/gpu/vr/libsdl-gamepad-proxy.so',
                       'tools/gpu/vr/gunspinning-primus-source/Makefile',
                       'tools/gpu/vr/gunspinning-primus-source/primus_vk.cpp']
    missing = [name for name in additional if not _inside(root, name).is_file()]
    if missing:
        raise ValueError('Missing workload files: ' + ', '.join(missing))
    return root


def python_packages(recipe):
    packages = []
    if {'desktop', 'vr'} & recipe['capabilities'].keys():
        packages += [('PIL', 'Pillow'), ('numpy', 'numpy')]
    if 'vr' in recipe['capabilities']:
        packages.append(('zstandard', 'zstandard'))
    return packages


def inspect(template='coding', *, assets=None):
    recipe = Template(template).resolve()
    checks = []
    supported = platform.system() == 'Linux' and platform.machine() in ('x86_64', 'amd64')
    checks.append(Check('platform', 'Worker platform', 'pass' if supported else 'fail',
                        platform.system() + ' ' + platform.machine() +
                        ('' if supported else '; run setup on a Linux x86-64 worker')))
    # The subprocess owns the temporary namespace. The doctor process keeps its identity.
    ok, detail = run_probe([sys.executable, '-c',
        'import ctypes,os; c=ctypes.CDLL(None,use_errno=True); '
        'r=c.unshare(0x10000000); '
        'print("Available" if r==0 else os.strerror(ctypes.get_errno())); '
        'raise SystemExit(0 if r==0 else 1)']) if supported else (False, 'Unsupported worker platform')
    checks.append(Check('userns', 'Container permissions', 'pass' if ok else 'fail',
        detail if ok else detail + '. Checking whether Apptainer can create the required namespace.'))
    apptainer = workspace.tool('apptainer')
    ok, detail = run_probe([apptainer, '--version']) if apptainer else (False, 'Apptainer is missing')
    checks.append(Check('apptainer', 'Apptainer', 'pass' if ok else 'fail', detail,
                        None if ok else 'Install or locate Apptainer'))
    try:
        root = validate_assets(assets or workspace.assets(), recipe)
        checks.append(Check('assets', 'Runtime files', 'pass', str(root)))
    except (OSError, ValueError, KeyError, TypeError, workspace.ResourceUnavailable) as error:
        root = None
        checks.append(Check('assets', 'Runtime files', 'fail', str(error), 'Install or repair runtime files'))
    if root and apptainer and checks[2].status == 'pass':
        with tempfile.TemporaryDirectory(prefix='sandweave-doctor-') as temporary:
            # Keep Apptainer's probe caches off the user's configured cache paths.
            env = {**os.environ, 'APPTAINER_CACHEDIR': temporary, 'APPTAINER_TMPDIR': temporary}
            try:
                process = subprocess.run([apptainer, 'exec', '--userns', '--contain', '--ipc',
                    '--cleanenv', '--no-home', str(root / 'tools/debian-trixie.sif'),
                    'sh', '-c', 'test ! -e /dev/kvm'], capture_output=True, text=True,
                    stdin=subprocess.DEVNULL, timeout=30, env=env)
                ok, detail = process.returncode == 0, (process.stdout + process.stderr).strip()[-2000:]
            except (OSError, subprocess.TimeoutExpired) as error:
                ok, detail = False, str(error)
        checks.append(Check('container', 'Container support', 'pass' if ok else 'fail',
                            'Apptainer started a temporary container' if ok else detail))
        if ok:
            # AppArmor may allow the installed Apptainer while restricting Python's probe.
            checks[1] = Check('userns', 'Container permissions', 'pass', 'Available through Apptainer')
    else:
        checks.append(Check('container', 'Container support', 'skip', 'Waiting for Apptainer and runtime files'))
    if checks[1].status == 'fail':
        checks[1].detail += ' Sandweave cannot change host policy without administrator access.'
    packages = python_packages(recipe)
    missing = [package for module, package in packages if importlib.util.find_spec(module) is None]
    checks.append(Check('python', 'Python packages', 'fail' if missing else 'pass',
                        ', '.join(missing) + ' missing' if missing else 'Required packages are installed',
                        'Install missing Python packages' if missing else None))
    if recipe.get('resources', {}).get('gpu'):
        # Reuse the runtime's allocation/device checks, not visibility from nvidia-smi alone.
        try:
            spec = importlib.util.spec_from_file_location('_sandweave_doctor_gpu', workspace.engine_sources() / 'gvisor_gpu.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            devices = module.eligible_devices()
            if not devices:
                raise ValueError('No GPU is allocated to this worker')
            for device in devices:
                module.allocated_device(device)
            ok, detail = True, str(len(devices)) + ' eligible GPU(s)'
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            ok, detail = False, str(error)
        checks.append(Check('gpu', 'NVIDIA GPU', 'pass' if ok else 'fail', detail))
    if 'vr' in recipe['capabilities']:
        ffmpeg = workspace.tool('ffmpeg')
        ok, detail = False, 'FFmpeg is missing'
        if ffmpeg:
            ok, detail = run_probe([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=s=16x16',
                                   '-frames:v', '1', '-c:v', 'libx264', '-f', 'null', '-'])
        checks.append(Check('ffmpeg', 'VR video export', 'pass' if ok else 'fail',
                            'FFmpeg with libx264 is available' if ok else detail,
                            None if ok else 'Install FFmpeg for Sandweave'))
    return checks


def show(checks, template):
    print(f'\nSandweave · {template}\n')
    for check in checks:
        icon = {'pass': 'ok', 'fail': '!!', 'skip': '--'}[check.status]
        print(f'  [{icon}] {check.title}')
        print('       ' + check.detail.replace('\n', '\n       '))
    print()


def passed(checks):
    return all(check.status == 'pass' for check in checks)


def _questionary():
    import questionary
    return questionary


def choose(message, choices):
    q = _questionary()
    answer = q.select(message, choices=[q.Choice(title, value=value) for title, value in choices]).ask()
    if answer is None:
        raise KeyboardInterrupt
    return answer


def confirm(message):
    answer = _questionary().confirm(message, default=False).ask()
    if answer is None:
        raise KeyboardInterrupt
    return answer


def ask_path(message, *, default=''):
    answer = _questionary().path(message, default=str(default)).ask()
    if answer is None:
        raise KeyboardInterrupt
    if not answer.strip():
        raise ValueError('Enter a directory path')
    return Path(answer).expanduser().resolve()


def install_packages(packages):
    cache = workspace.home() / 'downloads/python-cache'
    cache.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, 'UV_CACHE_DIR': str(cache / 'uv'),
                   'PIP_CACHE_DIR': str(cache / 'pip'), 'TMPDIR': str(cache)}
    uv = workspace.tool('uv')
    if uv:
        command = [uv, 'pip', 'install', '--python', sys.executable, *packages]
    elif importlib.util.find_spec('pip') is not None:
        command = [sys.executable, '-m', 'pip', 'install', *packages]
    else:
        # uv-created environments commonly omit pip. Bootstrap in this exact
        # interpreter; never invoke a different environment's `pip` executable.
        if importlib.util.find_spec('ensurepip') is None:
            raise ValueError('This Python has neither uv, pip nor ensurepip. Install uv, then run setup again.')
        run_logged([sys.executable, '-m', 'ensurepip'], workspace.home(),
                   label='Prepare Python installer', env=environment)
        command = [sys.executable, '-m', 'pip', 'install', *packages]
    try:
        run_logged(command, workspace.home(), label='Install Python packages', env=environment)
    except KeyboardInterrupt:
        print('Installation interrupted; some packages may already be installed. Run sandweave doctor to check.', file=sys.stderr)
        raise
    importlib.invalidate_caches()


def link_tool(name, source):
    source = Path(source).resolve()
    if not source.is_file() or not os.access(source, os.X_OK):
        raise ValueError('Tool is not executable: ' + str(source))
    directory = workspace.home() / 'bin'
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / name
    if destination.exists() and not destination.is_symlink():
        raise ValueError('Refusing to replace an existing file: ' + str(destination))
    temporary = directory / ('.' + name + '-' + uuid.uuid4().hex)
    try:
        temporary.symlink_to(source)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def install_apptainer():
    if not workspace.tool('bash'):
        raise ValueError('The Apptainer installer requires Bash')
    directory = workspace.home() / 'tools'
    directory.mkdir(parents=True, exist_ok=True)
    # Each attempt has its own directory; failed installations cannot overwrite a working one.
    destination = Path(tempfile.mkdtemp(prefix='apptainer-', dir=directory))
    # Provide package readers even on Debian/uv installs without RPM utilities.
    # These wrappers are private to this one pinned installer invocation.
    helpers = destination / 'installer-bin'
    helpers.mkdir()
    missing = [name for name in ('curl', 'rpm2cpio', 'cpio') if not workspace.tool(name)]
    if 'rpm2cpio' in missing and importlib.util.find_spec('zstandard') is None:
        install_packages(['zstandard'])
    for name in missing:
        wrapper = helpers / name
        wrapper.write_text('#!/bin/sh\nexec ' + shlex.join([
            sys.executable, '-m', 'sandweave.installer_tools', name]) + ' "$@"\n')
        wrapper.chmod(0o755)
    with Stage('Download Apptainer installer', unit='bytes') as progress:
        with urllib.request.urlopen(APPTAINER_INSTALLER, timeout=30) as response:
            script = response.read(1024 * 1024)
            progress.update(completed=len(script), detail='Verifying installer')
        if hashlib.sha256(script).hexdigest() != APPTAINER_INSTALLER_SHA256:
            raise ValueError('Apptainer installer checksum mismatch')
    path = destination / 'install.sh'
    path.write_bytes(script)
    try:
        run_logged(['bash', '-o', 'pipefail', str(path), '-v', APPTAINER_VERSION, str(destination / 'runtime')],
                   workspace.home(), label='Install Apptainer',
                   env={**os.environ, 'PATH': str(helpers) + os.pathsep + workspace.tool_path(),
                        'TMPDIR': str(destination)})
    except KeyboardInterrupt:
        print('Installation interrupted; partial files remain in ' + str(destination), file=sys.stderr)
        raise
    # The pinned upstream wrappers leave executable paths unquoted. Storage
    # paths containing spaces must remain single arguments in those wrappers.
    runtime = destination / 'runtime'
    for wrapper in (runtime / 'bin/apptainer', runtime / 'x86_64/utils/bin/.wrapper',
                    runtime / 'x86_64/libexec/apptainer/bin/.wrapper'):
        text = wrapper.read_text()
        text = text.replace('realpath $0', 'realpath "$0"')
        text = text.replace('exec $APPTDIR/bin/apptainer', 'exec "$APPTDIR/bin/apptainer"')
        text = text.replace('$REALME "$@"', '"$REALME" "$@"')
        wrapper.write_text(text)
    executable = destination / 'runtime/bin/apptainer'
    ok, detail = run_probe([str(executable), '--version'])
    if not ok:
        raise ValueError('Installed Apptainer did not start: ' + detail)
    link_tool('apptainer', executable)


def repair(name, template, *, yes=False, assets=None):
    recipe = Template(template).resolve()
    if name == 'assets':
        from types import SimpleNamespace
        args = SimpleNamespace(yes=yes, directory=None, assets=assets, game_archive=None)
        return setup_worker(args, template, interactive=not yes) == 0
    elif name == 'python':
        missing = [package for module, package in python_packages(recipe) if importlib.util.find_spec(module) is None]
        if not missing:
            return True
        if not yes and not confirm('Install ' + ', '.join(missing) + ' into ' + sys.prefix + '?'):
            return False
        install_packages(missing)
    elif name == 'ffmpeg':
        if not yes and not confirm('Install imageio-ffmpeg into ' + sys.prefix + ' and register its FFmpeg for Sandweave?'):
            return False
        install_packages(['imageio-ffmpeg>=0.6,<0.7'])
        import imageio_ffmpeg
        link_tool('ffmpeg', imageio_ffmpeg.get_ffmpeg_exe())
    elif name == 'apptainer':
        action = 'install' if yes else choose('Repair Apptainer', [
            ('Install Apptainer ' + APPTAINER_VERSION + ' in ' + str(workspace.home() / 'tools'), 'install'),
            ('Use an existing Apptainer executable', 'path'), ('Cancel', 'cancel')])
        if action == 'cancel':
            return False
        if action == 'path':
            executable = ask_path('Apptainer executable:')
            ok, detail = run_probe([str(executable), '--version'])
            if not ok:
                raise ValueError(detail)
            link_tool('apptainer', executable)
        else:
            if not yes and not confirm('Download the upstream installer and packages into Sandweave\'s tool directory?'):
                return False
            install_apptainer()
    else:
        raise ValueError('No automatic repair for ' + name)
    return True


def smoke_test(template='coding'):
    """Check the selected workload using a sandbox owned by this invocation."""
    from . import Sandbox
    with Sandbox(template=template, startup_timeout=300) as env:
        result = env.run("python -c 'print(2 + 2)'", timeout=10, check=True)
        if result.stdout.strip() != '4':
            raise ValueError('Sandbox returned an unexpected command result')
        if 'desktop' in env.capabilities:
            env.desktop.step({'keyboard': {'keys': ['shift']}})
        if 'vr' in env.capabilities:
            observation = env.vr.observe()
            if observation.left.size == 0 or observation.right.size == 0:
                raise ValueError('VR observation must contain both eye images')
        if env.spec['resources']['gpu']:
            env.run('nvidia-smi', timeout=20, check=True)
    print(template + ' sandbox check passed; the test sandbox has been released.')


def known_sources():
    try:
        current = [workspace.assets()]
    except workspace.ResourceUnavailable:
        current = []
    return list(dict.fromkeys([*current, *asset_candidates()]))


def install_runtime(template, directory, *, assets=None, sources=(), game_archive=None, yes=False, build=False):
    from .installation import import_runtime, validate_installation, needs_helpers
    from .bootstrap import Builder
    recipe = Template(template).resolve()
    selected = assets or os.environ.get('SANDWEAVE_ASSETS')
    if build and selected:
        raise ValueError('--build cannot be combined with an explicit runtime source')
    candidates = [Path(selected).expanduser().resolve()] if selected else list(sources)
    if build:
        candidates = []
    core = None
    for source in candidates:
        try:
            validate_assets(source, recipe)
            if (source / 'installation.json').is_file():
                validate_installation(source)
        except (OSError, ValueError, KeyError, TypeError, workspace.ResourceUnavailable) as error:
            # A coding installation can supply the engine while a new workload
            # is built. Explicit invalid sources must not silently fall back.
            try:
                validate_assets(source, Template('coding').resolve())
                if (source / 'installation.json').is_file():
                    validate_installation(source)
                core = source
            except (OSError, ValueError, KeyError, TypeError, workspace.ResourceUnavailable):
                if selected:
                    raise ValueError('The runtime source is incomplete: ' + str(source) + '\n' + str(error)) from error
            continue
        print('Installing runtime files from ' + str(source), flush=True)
        if ((source / 'installation.json').is_file() and source.is_relative_to(Path(directory) / 'assets')
                and not needs_helpers(source, recipe)):
            return source
        # Copy/space failures belong to the destination. They must not trigger
        # an unrelated source rebuild or a fallback into the home directory.
        return import_runtime(source, directory, recipe)
    profile = workload(recipe)
    from . import releases
    host = releases.check_host(directory)
    if 'gunspinning' in profile:
        from .vr_installation import GUNSPINNING_SHA256
        cached = Path(directory) / 'downloads/gunspinning-vr-linux.zip'
        if game_archive:
            archive = Path(game_archive).expanduser().resolve()
        elif cached.is_file():
            archive = cached
        elif yes:
            raise ValueError('GunSpinning needs its official Linux ZIP. Download it from '
                             'https://demonixis.itch.io/gunspinning-vr and pass --game-archive PATH.')
        else:
            print('Download gunspinning-vr-linux.zip from https://demonixis.itch.io/gunspinning-vr. '
                  'Choose the free download, then select that file here.')
            archive = ask_path('GunSpinning Linux ZIP:')
        workspace._immutable(archive, cached, sha256=GUNSPINNING_SHA256)
    engine = None
    if core is not None:
        core = import_runtime(core, directory, Template('coding').resolve())
    elif not build:
        engine = releases.install(directory, host)
    print('Preparing ' + profile + (' with the downloaded engine.' if engine else ' from upstream sources. The first build can take a while.'), flush=True)
    if engine is not None:
        return Builder(directory).build(profile, recipe, base=core, engine=engine)
    return Builder(directory).build(profile, recipe, base=core)


def setup_worker(args, template, interactive):
    from .installation import destination
    yes = getattr(args, 'yes', False)
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise ValueError('Run setup on a Linux x86-64 worker')
    selected = getattr(args, 'directory', None)
    explicit = os.environ.get('SANDWEAVE_HOME')
    if selected and explicit and Path(selected).expanduser().resolve() != Path(explicit).expanduser().resolve():
        raise ValueError('--directory conflicts with SANDWEAVE_HOME; update that variable first')
    # Resolve old sources before temporarily selecting new, possibly empty storage.
    sources = known_sources()
    previous = configuration()
    if not selected:
        default = workspace.home() if explicit or previous.get('assets') else Path.cwd() / '.sandweave'
        if interactive and not yes:
            selected = ask_path('Where should Sandweave store its files?', default=default)
        else:
            selected = default
    if explicit and Path(selected).expanduser().resolve() != Path(explicit).expanduser().resolve():
        raise ValueError('The selected directory conflicts with SANDWEAVE_HOME; update that variable first')
    selected = destination(selected)
    with workspace.locked(selected / '.setup.lock'):
        if getattr(args, '_automatic', False):
            from .sandbox.preparation import available
            # Another constructor may have completed this installation while
            # this process waited for the same storage lock.
            if available(Template(template).resolve(), selected) is not None:
                return 0
        return _setup_selected(args, template, interactive, selected, previous, sources)


def _setup_selected(args, template, interactive, selected, previous, sources):
    from .installation import publish, using_directory
    yes = getattr(args, 'yes', False)
    automatic = getattr(args, '_automatic', False)
    # An invalid destination config is user work, not an empty config to replace.
    current = configuration(selected)
    pending = selected / 'setup.json'
    pending_info = json.loads(pending.read_text()) if pending.is_file() else {}
    if not isinstance(pending_info, dict):
        raise ValueError('Invalid setup progress file: ' + str(pending))
    preferred = [Path(value).expanduser().resolve() for value in
                 (pending_info.get('assets'), current.get('assets')) if isinstance(value, str) and value]
    sources = list(dict.fromkeys([*preferred, *sources]))
    print('Sandweave files: ' + str(selected), flush=True)
    print('Setup logs: ' + str(selected / 'logs/setup'), flush=True)
    assets = getattr(args, 'assets', None)
    if automatic and os.environ.get('SANDWEAVE_ASSETS'):
        from .sandbox.preparation import source
        # Re-read after taking setup.lock: another template may have extended
        # this source while this installer waited.
        assets, _ = source(selected)
    if assets and interactive and not yes and not confirm('Install runtime files from ' + str(assets) + '?'):
        print('Setup cancelled before installing runtime files.')
        return 1
    with using_directory(selected):
        apptainer = workspace.tool('apptainer')
        if not apptainer or not run_probe([apptainer, '--version'])[0]:
            if not repair('apptainer', template, yes=yes):
                return 1
        installed = install_runtime(template, selected, assets=assets, sources=sources,
                                    game_archive=getattr(args, 'game_archive', None), yes=yes,
                                    build=getattr(args, 'build', False))
        workspace.atomic_json(pending, {'assets': str(installed), 'template': template, 'status': 'checking'})
        missing = [package for module, package in python_packages(Template(template).resolve())
                   if importlib.util.find_spec(module) is None]
        if missing and not repair('python', template, yes=yes):
            return 1
        # Check the actual selected install. An inherited source override must
        # not make the disposable acceptance check use a different directory.
        previous_assets = os.environ.get('SANDWEAVE_ASSETS')
        os.environ['SANDWEAVE_ASSETS'] = str(installed)
        try:
            with Stage('Check installation'):
                checks = inspect(template, assets=installed)
            if automatic:
                # Allocation checks belong to the requested Sandbox, whose
                # resource overrides may differ from the built-in defaults.
                checks = [check for check in checks if check.name != 'gpu']
            for check in checks:
                if check.fix and check.name not in ('assets', 'apptainer', 'python'):
                    repair(check.name, template, yes=yes)
            with Stage('Check installation'):
                checks = inspect(template, assets=installed)
            if automatic:
                checks = [check for check in checks if check.name != 'gpu']
            show(checks, template)
            if not passed(checks):
                print('Setup could not complete. Runtime files remain in ' + str(installed) + '.')
                return 1
            # The environment overrides select this candidate for its worker.
            # Leave the saved configuration intact until acceptance succeeds.
            if not automatic:
                from .sandbox.preparation import checking
                with Stage('Prepare worker files'):
                    workspace.prepare()
                # This candidate has already been installed and checked. Its
                # disposable Sandbox must not recursively acquire setup.lock.
                with checking(selected, installed), Stage('Test ' + template + ' sandbox'):
                    smoke_test(template)
        finally:
            if previous_assets is None:
                os.environ.pop('SANDWEAVE_ASSETS', None)
            else:
                os.environ['SANDWEAVE_ASSETS'] = previous_assets
    # Global location changes only after the selected installation completes.
    publish(selected, installed, previous=previous, template=template,
            source_identity=getattr(args, '_source_identity', None))
    workspace.atomic_json(pending, {'assets': str(installed), 'template': template,
                                    'status': 'installed' if automatic else 'ready'})
    print(('Installation complete. ' if automatic else 'Setup complete. ') + 'Sandweave files: ' + str(selected))
    return 0


def main(args):
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not getattr(args, 'check', False) and not getattr(args, 'json', False)
    setup = args.operation == 'setup'
    yes = getattr(args, 'yes', False)
    template = args.template or configuration().get('onboarding_template', 'coding')
    if setup and not args.template and interactive and not yes:
        template = choose('What do you want to start with? (You can add more later)',
                          [(label, name) for name, label in PROFILES.items()])
    if setup and not interactive and not yes:
        print('Setup needs a terminal. For scripts, use sandweave setup --yes --template coding.', file=sys.stderr)
        return 1
    assets = getattr(args, 'assets', None)
    if assets and os.environ.get('SANDWEAVE_ASSETS') and Path(assets).expanduser().resolve() != Path(os.environ['SANDWEAVE_ASSETS']).expanduser().resolve():
        raise ValueError('--assets conflicts with SANDWEAVE_ASSETS; remove or update that environment variable first')
    if setup:
        return setup_worker(args, template, interactive)
    checks = inspect(template, assets=assets)
    if getattr(args, 'json', False):
        print(json.dumps({'template': template, 'passed': passed(checks), 'checks': [asdict(c) for c in checks]}, indent=2))
        return 0 if passed(checks) else 1
    show(checks, template)
    if not interactive:
        return 0 if passed(checks) else 1
    while True:
        choices = [(c.fix, c.name) for c in checks if c.fix]
        choices += [('Check again', 'recheck')]
        if passed(checks):
            choices.append(('Test a disposable ' + template + ' sandbox', 'smoke'))
        choices += [('Choose another workload', 'template'), ('Exit', 'exit')]
        action = choose('Next action', choices)
        if action == 'exit':
            return 0 if passed(checks) else 1
        try:
            if action == 'template':
                template = choose('What do you want to start with? (You can add more later)',
                                  [(label, name) for name, label in PROFILES.items()])
            elif action == 'smoke':
                smoke_test(template)
            elif action != 'recheck':
                repair(action, template)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            print('Repair failed: ' + str(error), file=sys.stderr)
        checks = inspect(template)
        show(checks, template)

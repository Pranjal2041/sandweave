"""Local installation checks and user-selected repairs for setup and doctor."""
from dataclasses import asdict, dataclass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import uuid

from .sandbox import workspace
from .templates.resolve import Template

APPTAINER_VERSION = '1.5.3'
APPTAINER_INSTALLER = ('https://raw.githubusercontent.com/apptainer/apptainer/'
                      'v1.5.3/tools/install-unprivileged.sh')
APPTAINER_INSTALLER_SHA256 = 'a097956eafa6ab3dd843ee429d0b774ed029753bfadf013b8046556228fac6f2'
PROFILES = {'coding': 'Code', 'gnome': 'Desktop', 'cuda': 'CUDA',
            'vr/gunspinning': 'VR: GunSpinning', 'vr/opensaber': 'VR: Open Saber',
            'games/gunspinning-gamepad': 'GunSpinning: gamepad'}


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
    """Keep data with the selected installation, preserving explicit locations."""
    location = workspace.default_home() / 'location.json'
    if os.environ.get('SANDWEAVE_HOME') or location.exists():
        save_configuration(assets=str(root))
    else:
        destination = Path(root) / '.sandweave'
        with workspace.locked(location.with_suffix('.lock')):
            if location.exists():
                save_configuration(assets=str(root))
            else:
                old = configuration()
                destination.mkdir(mode=0o700, exist_ok=True)
                if destination.stat().st_uid != os.getuid():
                    raise ValueError('Sandweave data directory belongs to another user: ' + str(destination) +
                                     '. Select your own directory with SANDWEAVE_HOME.')
                path = destination / 'config.json'
                with workspace.locked(path.with_suffix('.lock')):
                    current = configuration(destination)
                    merged = {**old, **current, 'assets': str(root)}
                    if 'targets' in old or 'targets' in current:
                        merged['targets'] = {**old.get('targets', {}), **current.get('targets', {})}
                    workspace.atomic_json(path, merged)
                # Publish the location only after its configuration is ready.
                workspace.atomic_json(location, {'path': str(destination.resolve())})
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


def validate_assets(root, recipe):
    """Check launch inputs and runtime hashes without hashing multi-GB images."""
    root = Path(root).expanduser().resolve()
    required = ['tools/debian-trixie.sif', 'tools/bench', 'tools/seccomp-trap',
                'tools/gs-base-probe', 'tools/gvisor-socket/runtime.json',
                'images/gvisor-ubuntu-ready-ae303ca.erofs']
    missing = [name for name in required if not (root / name).is_file()]
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
        with _inside(build, name).open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                raise ValueError('runtime checksum mismatch: ' + name)
    registry_path = root / 'sandweave-assets.json'
    registry = json.loads(registry_path.read_text()) if registry_path.exists() else {}
    for name, info in registry.get('images', {}).items():
        if _inside(root, name).stat().st_size != info['size']:
            raise ValueError('image size differs from its manifest: ' + name)
    if recipe.get('base_snapshot'):
        info = registry.get('snapshots', {}).get(recipe['base_snapshot'])
        if not info:
            raise ValueError('Missing prepared game base: ' + recipe['base_snapshot'])
        snapshot = _inside(root, info['path'])
        manifest = json.loads((snapshot / 'snapshot-manifest.json').read_text())
        if manifest['snapshot_id'] != info['snapshot_id']:
            raise ValueError('game base differs from its registered snapshot')
        if manifest.get('kind') != 'filesystem' or 'rootfs-upper.tar' not in manifest.get('files', {}):
            raise ValueError('game base must contain a filesystem snapshot')
        for name, item in manifest['files'].items():
            if _inside(snapshot, name).stat().st_size != item['size']:
                raise ValueError('game base payload size mismatch: ' + name)
        base = manifest['base_image']
        if _inside(root, base['path']).stat().st_size != base['size']:
            raise ValueError('game base image size mismatch')
        dependency = manifest['runtime']
        for name, expected in dependency['sha256'].items():
            with _inside(_inside(root, dependency['path']), name).open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                    raise ValueError('game base runtime checksum mismatch: ' + name)
    additional = []
    if 'desktop' in recipe['capabilities']:
        additional += ['tools/fast-io/bridge', 'tools/fast-io/libxcb-xtest.so.0']
    if 'gunspinning' in recipe['name']:
        additional += ['tools/gpu/vr/gunspinning-linux-2.0.1/GunSpinningVR',
                       'tools/gpu/vr/gunspinning-linux-2.0.1/GunSpinningVR_Data/globalgamemanagers',
                       'tools/gpu/vr/xrizer-v0.5/bin/linux64/vrclient.so',
                       'tools/gpu/vr/libsdl-gamepad-proxy.so',
                       'tools/gpu/vr/gunspinning-primus-source/Makefile',
                       'tools/gpu/vr/gunspinning-primus-source/primus_vk.cpp']
    missing = [name for name in additional if not (root / name).is_file()]
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
        checks.append(Check('assets', 'Runtime files', 'fail', str(error), 'Find prepared runtime files'))
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
        checks.append(Check('container', 'Container launch', 'pass' if ok else 'fail',
                            'Host container started without KVM' if ok else detail))
        if ok:
            # AppArmor may allow the installed Apptainer while restricting Python's probe.
            checks[1] = Check('userns', 'Container permissions', 'pass', 'Available through Apptainer')
    else:
        checks.append(Check('container', 'Container launch', 'skip', 'Waiting for Apptainer and runtime files'))
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


def ask_path(message):
    answer = _questionary().path(message).ask()
    if answer is None:
        raise KeyboardInterrupt
    return Path(answer).expanduser().resolve()


def install_packages(packages):
    command = [sys.executable, '-m', 'pip', 'install', *packages]
    print('Running ' + shlex.join(command), flush=True)
    try:
        subprocess.run(command, check=True)
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
    missing = [name for name in ('bash', 'curl', 'rpm2cpio', 'cpio') if not shutil.which(name)]
    if missing:
        raise ValueError('The upstream installer needs ' + ', '.join(missing) +
                         '. Use an existing Apptainer installation, or prepare it on another compatible machine.')
    directory = workspace.home() / 'tools'
    directory.mkdir(parents=True, exist_ok=True)
    # Each attempt has its own directory; failed installations cannot overwrite a working one.
    destination = Path(tempfile.mkdtemp(prefix='apptainer-', dir=directory))
    with urllib.request.urlopen(APPTAINER_INSTALLER, timeout=30) as response:
        script = response.read(1024 * 1024)
    if hashlib.sha256(script).hexdigest() != APPTAINER_INSTALLER_SHA256:
        raise ValueError('Apptainer installer checksum mismatch')
    path = destination / 'install.sh'
    path.write_bytes(script)
    try:
        subprocess.run(['bash', str(path), '-v', APPTAINER_VERSION, str(destination / 'runtime')], check=True)
    except KeyboardInterrupt:
        print('Installation interrupted; partial files remain in ' + str(destination), file=sys.stderr)
        raise
    executable = destination / 'runtime/bin/apptainer'
    ok, detail = run_probe([str(executable), '--version'])
    if not ok:
        raise ValueError('Installed Apptainer did not start: ' + detail)
    link_tool('apptainer', executable)


def repair(name, template, *, yes=False, assets=None):
    recipe = Template(template).resolve()
    if name == 'assets':
        if os.environ.get('SANDWEAVE_ASSETS'):
            # Writing config cannot override this environment variable in other shells.
            root = validate_assets(os.environ['SANDWEAVE_ASSETS'], recipe)
        elif assets:
            root = validate_assets(assets, recipe)
        else:
            candidates = []
            try:
                known = [workspace.assets()]
            except workspace.ResourceUnavailable:
                known = []
            for path in dict.fromkeys([*known, *asset_candidates()]):
                try:
                    candidates.append(validate_assets(path, recipe))
                except (OSError, ValueError, KeyError, TypeError):
                    continue
            if yes:
                if len(candidates) != 1:
                    raise ValueError('No unique prepared runtime found. Run setup interactively to select one. '
                                     'A public runtime download bundle is not available yet.')
                root = candidates[0]
            else:
                options = [(str(path), path) for path in candidates]
                options += [('Choose a different directory', 'path'), ('Cancel', 'cancel')]
                root = choose('Use these runtime files?', options)
                if root == 'cancel':
                    return False
                if root == 'path':
                    print('Choose a prepared installation. A public runtime download bundle is not available yet.')
                    root = ask_path('Prepared runtime directory:')
                root = validate_assets(root, recipe)
        save_runtime_location(root)
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


def smoke_test():
    """Only the disposable coding sandbox belongs to this check."""
    from . import Sandbox
    with Sandbox(template='coding', startup_timeout=120) as env:
        result = env.run("python -c 'print(2 + 2)'", timeout=10)
        if result.stdout.strip() != '4':
            raise ValueError('Coding sandbox returned an unexpected result')
    print('Coding sandbox check passed; the test sandbox has been released.')


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
    checks = inspect(template, assets=assets)
    if getattr(args, 'json', False):
        print(json.dumps({'template': template, 'passed': passed(checks), 'checks': [asdict(c) for c in checks]}, indent=2))
        return 0 if passed(checks) else 1
    show(checks, template)
    if setup:
        # Setup also persists auto-discovery so later invocations work from another directory.
        repairs = ['assets', *[c.name for c in checks if c.fix and c.name != 'assets']]
        for name in repairs:
            if not yes and name == 'assets' and assets and not confirm('Use runtime files from ' + str(assets) + '?'):
                print('Setup cancelled before changing the runtime selection.')
                return 1
            try:
                applied = repair(name, template, yes=yes, assets=assets)
                if name == 'assets' and not applied:
                    print('Setup cancelled before changing the runtime selection.')
                    return 1
                if applied:
                    print('Updated ' + name + '. Rechecking...', flush=True)
                    checks = inspect(template, assets=assets)
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
                print('Repair failed: ' + str(error), file=sys.stderr)
                if name == 'assets':
                    print('Setup stopped before staging files. Select a writable data directory with SANDWEAVE_HOME.')
                    return 1
        save_configuration(onboarding_template=template)
        checks = inspect(template, assets=assets)
        show(checks, template)
        if passed(checks):
            print('Preparing worker files...', flush=True)
            workspace.prepare()
            smoke_test()
            print('Setup complete.')
            return 0
        print('Setup is incomplete. Resolve the remaining checks, then run sandweave doctor.')
        return 1
    if not interactive:
        return 0 if passed(checks) else 1
    while True:
        choices = [(c.fix, c.name) for c in checks if c.fix]
        choices += [('Check again', 'recheck')]
        if passed(checks):
            choices.append(('Test a disposable coding sandbox', 'smoke'))
        choices += [('Choose another workload', 'template'), ('Exit', 'exit')]
        action = choose('Next action', choices)
        if action == 'exit':
            return 0 if passed(checks) else 1
        try:
            if action == 'template':
                template = choose('What do you want to start with? (You can add more later)',
                                  [(label, name) for name, label in PROFILES.items()])
            elif action == 'smoke':
                smoke_test()
            elif action != 'recheck':
                repair(action, template)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            print('Repair failed: ' + str(error), file=sys.stderr)
        checks = inspect(template)
        show(checks, template)

"""Install runtime inputs into user-selected storage, before publishing configuration."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid

from .sandbox import workspace
from .setup_progress import Stage


@contextmanager
def using_directory(directory):
    """Keep every setup download and repair in the selected destination."""
    previous = os.environ.get('SANDWEAVE_HOME')
    os.environ['SANDWEAVE_HOME'] = str(directory)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop('SANDWEAVE_HOME', None)
        else:
            os.environ['SANDWEAVE_HOME'] = previous


def destination(path):
    root = Path(path).expanduser().resolve()
    # Apptainer bind specifications use these delimiters, even without a shell.
    if any(character in str(root) for character in (':', ',', '\n', '\0')):
        raise ValueError('Storage paths cannot contain colons, commas, newlines or NUL characters')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.stat().st_uid != os.getuid():
        raise ValueError('Choose a storage directory owned by your user: ' + str(root))
    # Check actual creation, rather than os.access (which misses quotas and ACLs).
    with tempfile.TemporaryFile(dir=root):
        pass
    return root


def publish(directory, assets, *, previous=None, template=None, source_identity=None):
    from .onboarding import configuration
    root = destination(directory)
    explicit = os.environ.get('SANDWEAVE_HOME')
    if explicit and Path(explicit).expanduser().resolve() != root:
        raise ValueError('--directory conflicts with SANDWEAVE_HOME; update that variable first')
    path = root / 'config.json'
    with workspace.locked(path.with_suffix('.lock')):
        current = configuration(root)
        old = previous or {}
        merged = {**old, **current, 'assets': str(Path(assets).resolve())}
        if 'targets' in old or 'targets' in current:
            merged['targets'] = {**old.get('targets', {}), **current.get('targets', {})}
        if template is not None:
            merged['onboarding_template'] = template
        if source_identity is not None:
            merged['installed_sources'] = {**old.get('installed_sources', {}),
                **current.get('installed_sources', {}), source_identity: str(Path(assets).resolve())}
        workspace.atomic_json(path, merged)
    if not explicit:
        location = workspace.default_home() / 'location.json'
        with workspace.locked(location.with_suffix('.lock')):
            workspace.atomic_json(location, {'path': str(root)})


def inputs(source, recipe):
    """The transitive runtime inputs for one workload; no unrelated lab trees."""
    from .onboarding import _inside, workload
    source = Path(source).resolve()
    registry_path = source / 'sandweave-assets.json'
    original_registry = json.loads(registry_path.read_text()) if registry_path.is_file() else {}
    paths = {'tools/debian-trixie.sif'}
    # Lab diagnostics are optional; normal sandbox execution does not use them.
    paths.update('tools/' + name for name in ('bench', 'seccomp-trap', 'gs-base-probe')
                 if (source / 'tools' / name).is_file())
    paths.add(original_registry.get('default_image', 'images/gvisor-ubuntu-ready-ae303ca.erofs'))
    paths.add('tools/gvisor-socket/runtime.json')
    runtime = json.loads((source / 'tools/gvisor-socket/runtime.json').read_text())
    revisions = source / 'notes/source-revisions.json'
    if revisions.is_file():
        candidate = json.loads(revisions.read_text()).get('gvisor', {}).get('candidate_runtime_build')
        if candidate and (_inside(source, candidate) / 'manifest.json').is_file():
            runtime = {'path': candidate, 'sha256': json.loads(
                (_inside(source, candidate) / 'manifest.json').read_text())}
    if (source / 'installation.json').is_file():
        # Managed installations contain only installed workload inputs. Preserve
        # all of them when moving storage, including previously added templates.
        selected = [name for name in ('tools', 'images', 'snapshots') if (source / name).is_dir()]
        return selected, runtime, original_registry
    paths.add(runtime['path'])
    registry = original_registry
    registry = {'schema_version': 1,
                'default_image': original_registry.get('default_image', 'images/gvisor-ubuntu-ready-ae303ca.erofs'),
                'images': {name: info for name, info in registry.get('images', {}).items() if name in paths},
                'snapshots': {}}
    if recipe.get('base_snapshot'):
        original = json.loads(registry_path.read_text())
        name = recipe['base_snapshot']
        info = original['snapshots'][name]
        registry['snapshots'][name] = info
        paths.add(info['path'])
        if info.get('kind') != 'image':
            manifest = json.loads((_inside(source, info['path']) / 'snapshot-manifest.json').read_text())
            paths.add(manifest['base_image']['path'])
            paths.add(manifest['runtime']['path'])
            registry['images'][manifest['base_image']['path']] = {
                key: manifest['base_image'][key] for key in ('size', 'sha256')}
        else:
            registry['images'][info['path']] = original['images'][info['path']]
    # Host helpers run in the host image; GPU libraries run in the guest image.
    for name in ('network', 'erofs', 'helpers'):
        if (source / 'tools' / name).is_dir():
            paths.add('tools/' + name)
    if 'desktop' in recipe['capabilities']:
        paths.add('tools/fast-io')
    # These small optional compute inputs also serve a later CUDA template.
    if (source / 'tools/gpu/bin/cuda-checkpoint').is_file():
        paths.add('tools/gpu/bin/cuda-checkpoint')
    for path in (source / 'tools/gpu').glob('mps-rm-observer-*.so'):
        paths.add(str(path.relative_to(source)))
    if recipe.get('resources', {}).get('gpu'):
        for name in ('virtualgl', 'xcb-keysyms', 'compat', 'python'):
            if (source / 'tools/gpu' / name).exists():
                paths.add('tools/gpu/' + name)
    if 'gunspinning' in workload(recipe):
        paths.update('tools/gpu/vr/' + name for name in (
            'gunspinning-linux-2.0.1', 'xrizer-v0.5', 'libsdl-gamepad-proxy.so',
            'gunspinning-primus-source'))
    # The native command runtime is an optional input, installed on demand.
    if (source / 'tools/gvisor-builder.sif').is_file():
        paths.add('tools/gvisor-builder.sif')
    return sorted(paths), runtime, registry


def import_runtime(source, directory, recipe):
    """Copy/link a verified input set; publish no partial installation."""
    from .onboarding import _inside, validate_assets
    source = validate_assets(source, recipe)
    if (source / 'installation.json').is_file():
        validate_installation(source)
    store = Path(directory) / 'assets'
    store.mkdir(parents=True, exist_ok=True)
    selected, runtime, registry = inputs(source, recipe)
    identity = hashlib.sha256(json.dumps(
        {'source': workspace.asset_identity(source), 'inputs': selected,
         'runtime': runtime, 'registry': registry}, sort_keys=True).encode()).hexdigest()[:24]
    target = store / identity
    with workspace.locked(store / (identity + '.lock')):
        if target.exists():
            try:
                validate_assets(target, recipe)
                validate_installation(target)
                if needs_helpers(target, recipe):
                    raise ValueError('This worker needs installed network utilities')
                return target
            except (OSError, ValueError, KeyError, TypeError):
                # A damaged published directory may still be used by a worker.
                # Install a replacement separately; never rewrite that tree.
                target = store / (identity + '-' + uuid.uuid4().hex[:12])
        temporary = Path(tempfile.mkdtemp(prefix='.import-', dir=store))
        try:
            with Stage('Copy runtime files', total=len(selected), unit='inputs') as progress:
                for name in selected:
                    progress.update(detail=name)
                    workspace.stage_tree(_inside(source, name), temporary / name)
                    progress.update(advance=1)
            workspace.atomic_json(temporary / 'tools/gvisor-socket/runtime.json', runtime)
            workspace.atomic_json(temporary / 'sandweave-assets.json', registry)
            if needs_helpers(temporary, recipe):
                from .bootstrap import Builder
                (temporary / 'build-tmp').mkdir()
                Builder(directory).helpers(temporary)
                shutil.rmtree(temporary / 'build-tmp')
            with Stage('Verify runtime files'):
                validate_assets(temporary, recipe)
                record_installation(temporary)
            temporary.rename(target)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return target


def needs_helpers(root, recipe=None):
    helpers = Path(root) / 'tools/helpers'
    if recipe and 'desktop' in recipe['capabilities']:
        if not all((helpers / 'usr/include/X11' / name).is_file()
                   for name in ('keysymdef.h', 'XF86keysym.h')):
            return True
    for name in ('ip', 'passt'):
        if workspace.tool(name):
            continue
        if not any((helpers / directory / name).is_file() for directory in ('usr/bin', 'usr/sbin', 'bin', 'sbin')):
            return True
        if name == 'passt' and not (helpers / 'usr/bin/prlimit').is_file():
            return True
    return False


def record_installation(root):
    records = {}
    for path in sorted(Path(root).rglob('*')):
        if path.name == 'installation.json':
            continue
        if path.is_symlink():
            records[str(path.relative_to(root))] = {'link': os.readlink(path)}
        elif path.is_file():
            records[str(path.relative_to(root))] = {
                'size': path.stat().st_size, 'sha256': workspace.file_digest(path)}
    workspace.atomic_json(Path(root) / 'installation.json', {'schema_version': 1, 'files': records})


def validate_installation(root):
    root = Path(root).resolve()
    manifest = Path(root) / 'installation.json'
    if not manifest.is_file():
        raise ValueError('Incomplete runtime installation: ' + str(root))
    value = json.loads(manifest.read_text())
    if (not isinstance(value, dict) or value.get('schema_version') != 1
            or not isinstance(value.get('files'), dict) or not value['files']):
        raise ValueError('Invalid runtime installation manifest: ' + str(manifest))
    for name, info in value['files'].items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Invalid installed runtime path: ' + name)
        path = root / relative
        if not path.parent.resolve().is_relative_to(root):
            raise ValueError('Installed runtime traverses an external directory: ' + name)
        if not isinstance(info, dict):
            raise ValueError('Invalid installed runtime entry: ' + name)
        if 'link' in info:
            if not path.is_symlink() or os.readlink(path) != info['link']:
                raise ValueError('Installed runtime symlink changed: ' + name)
        elif not path.is_file() or path.stat().st_size != info['size'] or workspace.file_digest(path) != info['sha256']:
            raise ValueError('Installed runtime checksum mismatch: ' + name)

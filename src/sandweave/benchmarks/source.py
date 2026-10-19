"""Read pinned integration resources without modifying their checkout."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import types
import urllib.request

from ..sandbox.workspace import home, locked

CUA_REPOSITORY = 'cua-speedrun/cua-speedrun'
CUA_REVISION = 'be17c72c5efbb145d06f86028336fdf2743a3d98'
CUA_ARCHIVE_SHA256 = '19769c938fb64b514080dd592b5c5d93c0543ad5ec4f92069aaa0c9df9bbd369'
LEGACY_CUA_REVISION = '681f8dbc695ff3a7e3af2f532bec982725818211'
OSWORLD_REVISION = '315a7603173feadf1b8a85cbc006c93ffe1dc1a1'
SUBSETS = {'osworld-energy50-representative': 'osworld-50',
           'osworld-unanimous-295': 'osworld-offline'}


def cua_revision(reference):
    """Identify a validated local checkout or the downloaded public source."""
    if (reference / '.git').exists():
        return subprocess.run(['git', '-C', str(reference), 'rev-parse', 'HEAD'],
                              capture_output=True, text=True, check=True).stdout.strip()
    return CUA_REVISION


def subset_manifest(reference, name):
    public = reference / 'benchmarks' / SUBSETS[name] / 'benchmark-source.yaml'
    # Keep explicitly supplied legacy checkouts reproducible.
    return public if public.is_file() else reference / 'benchmarks' / name / 'benchmark-source.yaml'


def module(path):
    """Load a trusted source file without writing bytecode beside it."""
    path = Path(path)
    value = types.ModuleType('sandweave_source_' + hashlib.sha256(str(path).encode()).hexdigest()[:16])
    value.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), 'exec'), value.__dict__)
    return value


def cua(source=None):
    if source is not None:
        source = Path(source).expanduser().resolve()
        result = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'],
                                capture_output=True, text=True, check=True)
        if result.stdout.strip() not in (CUA_REVISION, LEGACY_CUA_REVISION):
            raise ValueError('OSWorld integration requires cua-speedrun commit ' + CUA_REVISION +
                             ' (or legacy cua-speed-run commit ' + LEGACY_CUA_REVISION + ')')
        changed = subprocess.run(['git', '-C', str(source), 'diff', '--name-only', 'HEAD', '--',
                                  'scripts', 'benchmarks', 'src/cua_speedrun/envs',
                                  'src/cua_speedrun/remote/osworld_modal_base.py'],
                                 capture_output=True, text=True, check=True)
        if changed.stdout.strip():
            raise ValueError('OSWorld reference inputs have local modifications')
        return source
    from ..bootstrap import extract_source
    directory = home() / 'benchmarks/sources'
    destination = directory / ('cua-' + CUA_REVISION)
    with locked(destination.with_suffix('.lock')):
        if destination.is_dir():
            return destination
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.cua-', dir=directory) as temporary:
            temporary = Path(temporary)
            archive = temporary / 'source.tar.gz'
            url = 'https://codeload.github.com/' + CUA_REPOSITORY + '/tar.gz/' + CUA_REVISION
            with urllib.request.urlopen(url, timeout=180) as response, archive.open('wb') as output:
                shutil.copyfileobj(response, output)
            with archive.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if digest != CUA_ARCHIVE_SHA256:
                raise ValueError('OSWorld integration source archive checksum mismatch')
            extract_source(archive, temporary / 'source')
            (temporary / 'source').rename(destination)
        return destination


def tasks(reference, name):
    """Materialize canonical task JSON and the exact selected upstream hooks."""
    import importlib.util
    if importlib.util.find_spec('yaml') is None:
        from ..onboarding import install_packages
        install_packages(['PyYAML'])
    from .benchmark import TaskSpec
    base_url = ('https://raw.githubusercontent.com/xlang-ai/OSWorld/' +
                OSWORLD_REVISION + '/evaluation_examples/')
    if name != 'osworld':
        selected = module(reference / 'scripts/build_osworld_subset.py')._read_spec(
            subset_manifest(reference, name))['tasks']
    else:
        with urllib.request.urlopen(base_url + 'test_all.json', timeout=60) as response:
            manifest = json.load(response)
        selected = [{'id': 'osworld_' + domain + '_' + identity}
                    for domain, identities in manifest.items() for identity in identities]
    root = home() / 'benchmarks/tasks' / cua_revision(reference) / name
    root.mkdir(parents=True, exist_ok=True)

    def fetch(item):
        identity = item['id']
        domain, uid = identity.removeprefix('osworld_').rsplit('_', 1)
        directory = root / (domain + '__' + uid)
        destination = directory / 'source.json'
        with locked(directory.with_suffix('.lock')):
            data = destination.read_bytes() if destination.is_file() else None
            expected = item.get('source_sha256')
            if data is None or (expected and hashlib.sha256(data).hexdigest() != expected):
                with urllib.request.urlopen(base_url + 'examples/' + domain + '/' + uid + '.json', timeout=60) as response:
                    source = json.load(response)
                data = (json.dumps(source, indent=2) + '\n').encode()
                if expected and hashlib.sha256(data).hexdigest() != expected:
                    raise ValueError('OSWorld task source checksum mismatch: ' + identity)
                directory.mkdir(parents=True, exist_ok=True)
                # Preserve the reference materializer's exact JSON bytes.
                temporary = destination.with_suffix('.tmp')
                temporary.write_bytes(data)
                os.replace(temporary, destination)
            source = json.loads(data)
            patch = reference / 'scripts/osworld_task_patches' / (directory.name + '.json')
            if patch.is_file():
                shutil.copyfile(patch, directory / 'setup-patch.json')
            else:
                (directory / 'setup-patch.json').unlink(missing_ok=True)
        return TaskSpec(identity, source['instruction'], {'domain': domain, 'osworld_id': uid,
                        'directory': str(directory)})

    with ThreadPoolExecutor(max_workers=8, thread_name_prefix='sandweave-task-source') as executor:
        return tuple(executor.map(fetch, selected))

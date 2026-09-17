"""Acquire new engine features without replacing a running sandbox's runtime."""
import json
import hashlib
from functools import lru_cache
from pathlib import Path
import shutil
import subprocess
import tempfile

from ...errors import UnsupportedFeature
from ...workspace import atomic_json, home, locked, stage_tree


def disk_runtime(root, snapshot=None):
    return feature_runtime(root, ['app-memory-directory'], snapshot)


def feature_runtime(root, features, snapshot=None):
    root = Path(root)
    manifest = json.loads((Path(snapshot) / 'snapshot-manifest.json').read_text()) if snapshot else None
    live = manifest is not None and manifest.get('kind', 'live') == 'live'
    pointer = root / 'tools/gvisor-socket/feature-runtime.json'
    with locked(pointer.with_suffix('.lock')):
        descriptor = (manifest['runtime'] if live else json.loads(
            (pointer if pointer.exists() else root / 'tools/gvisor-socket/runtime.json').read_text()))
        if supports(root, descriptor, features):
            return [] if live else ['--runtime-build', descriptor['path']]
        if live:
            raise UnsupportedFeature('the captured runtime does not support: ' + ', '.join(features))
        from .... import releases
        directory = home()
        engine = releases.install(directory, releases.check_host(directory))
        if engine is not None:
            candidate = json.loads((engine / 'tools/gvisor-socket/runtime.json').read_text())
            stage_tree(engine / candidate['path'], root / candidate['path'])
            if not supports(root, candidate, features):
                engine = None
        if engine is None:
            engine = build_features(directory)
        descriptor = json.loads((engine / 'tools/gvisor-socket/runtime.json').read_text())
        stage_tree(engine / descriptor['path'], root / descriptor['path'])
        if not supports(root, descriptor, features):
            raise UnsupportedFeature('the installed engine does not support: ' + ', '.join(features))
        atomic_json(pointer, descriptor)
        return ['--runtime-build', descriptor['path']]


def build_features(directory):
    """Share one source build across workers using the same installation."""
    from ....bootstrap import Builder, BUILDER, GVISOR_BASE, build_input
    digest = hashlib.sha256((BUILDER + GVISOR_BASE).encode())
    for name in ('gvisor-no-kvm-prototype.patch', 'bench.c', 'seccomp-trap.c', 'gs-base-probe.c'):
        digest.update(build_input(name).read_bytes())
    destination = directory / 'assets' / ('feature-engine-' + digest.hexdigest()[:24])
    with locked(destination.with_suffix('.lock')):
        if (destination / 'tools/gvisor-socket/runtime.json').is_file():
            return destination
        builder = Builder(directory)
        with tempfile.TemporaryDirectory(prefix='.feature-engine-', dir=destination.parent) as temporary:
            engine = Path(temporary) / 'build'
            for name in ('tools', 'input', 'build-tmp', 'runs'):
                (engine / name).mkdir(parents=True, exist_ok=True)
            image = builder.downloads / 'gvisor-builder.sif'
            builder.pull(BUILDER, image)
            shutil.copy2(image, engine / 'tools/gvisor-builder.sif')
            for name in ('bench.c', 'seccomp-trap.c', 'gs-base-probe.c'):
                shutil.copy2(build_input(name), engine / 'input' / name)
            builder.engine(engine)
            engine.rename(destination)
        return destination


def supports_disk(root, descriptor):
    return supports(root, descriptor, ['app-memory-directory'])


def supports(root, descriptor, features):
    import runtime_store
    runtime_store.validate(root, descriptor)
    return set(features) <= flags(str(root), descriptor['path'])


@lru_cache(maxsize=32)
def flags(root, runtime):
    # Runtime directories are immutable. Probe once per worker/build, rather
    # than starting another host container for every pool refill.
    command = [str(Path(root) / 'scripts/gvisor-host.sh'), '/lab/' + runtime + '/runsc', 'flags']
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    return frozenset(line.strip().split()[0].decode().lstrip('-')
                     for line in result.stdout.splitlines() if line.strip().startswith(b'-'))

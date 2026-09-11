"""Acquire new engine features without replacing a running sandbox's runtime."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from ...errors import UnsupportedFeature
from ...workspace import atomic_json, home, locked, stage_tree


def disk_runtime(root, snapshot=None):
    root = Path(root)
    manifest = json.loads((Path(snapshot) / 'snapshot-manifest.json').read_text()) if snapshot else None
    live = manifest is not None and manifest.get('kind', 'live') == 'live'
    pointer = root / 'tools/gvisor-socket/disk-runtime.json'
    with locked(pointer.with_suffix('.lock')):
        descriptor = (manifest['runtime'] if live else json.loads(
            (pointer if pointer.exists() else root / 'tools/gvisor-socket/runtime.json').read_text()))
        if supports_disk(root, descriptor):
            return [] if live else ['--runtime-build', descriptor['path']]
        if live:
            raise UnsupportedFeature('the captured runtime does not support disk memory')
        from .... import releases
        from ....bootstrap import Builder, BUILDER, build_input
        directory = home()
        engine = releases.install(directory, releases.check_host(directory))
        if engine is None:
            builder = Builder(directory)
            (directory / 'assets').mkdir(parents=True, exist_ok=True)
            engine = Path(tempfile.mkdtemp(prefix='.disk-engine-', dir=directory / 'assets'))
            for name in ('tools', 'input', 'build-tmp', 'runs'):
                (engine / name).mkdir(parents=True, exist_ok=True)
            image = builder.downloads / 'gvisor-builder.sif'
            builder.pull(BUILDER, image)
            shutil.copy2(image, engine / 'tools/gvisor-builder.sif')
            for name in ('bench.c', 'seccomp-trap.c', 'gs-base-probe.c'):
                shutil.copy2(build_input(name), engine / 'input' / name)
            builder.engine(engine)
        descriptor = json.loads((engine / 'tools/gvisor-socket/runtime.json').read_text())
        stage_tree(engine / descriptor['path'], root / descriptor['path'])
        if not supports_disk(root, descriptor):
            raise UnsupportedFeature('the installed engine does not provide disk-backed memory')
        atomic_json(pointer, descriptor)
        return ['--runtime-build', descriptor['path']]


def supports_disk(root, descriptor):
    import runtime_store
    runtime_store.validate(root, descriptor)
    command = [str(root / 'scripts/gvisor-host.sh'), '/lab/' + descriptor['path'] + '/runsc', 'flags']
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    return b'-app-memory-directory' in result.stdout

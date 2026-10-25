"""Build a template's Modal image from the same pinned installation used by `sandweave setup`.

Sandweave's gVisor images are built by install-sandbox-base.sh inside the engine
and exported as EROFS. Here the same script and inputs run in Modal's image
builder on the same Ubuntu 22.04 base, and its GPU outputs (VirtualGL, the visual
order shim) are placed where the engine would mount them (/opt/engine-gpu).
Guest programs for this runtime are added last, so changing them rebuilds only
small layers. Modal caches every layer by its definition.
"""
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

from ...errors import UnsupportedFeature

# Docker Hub index digest of ubuntu:22.04, the base Sandweave's installation expects.
UBUNTU = 'ubuntu:22.04@sha256:0a9887931b0e38af2e3487ed39e29436b0535c4f3f52e57745baaf79d1898956'
GUEST = {'guest_boot.py': '/opt/sandweave/guest_boot.py', 'guest_vr.py': '/opt/sandweave/guest_vr.py'}
ENGINE_GUEST = {'gvisor-guest-gpu.sh': '/usr/local/bin/engine-gpu', 'gvisor-guest-gpu-gl.sh': '/usr/local/bin/engine-gpu-gl'}


def profile(recipe):
    from ....onboarding import workload
    name = workload(recipe)
    if not name.startswith('vr/') or 'gunspinning' in name:
        raise UnsupportedFeature('runtime="modal" currently builds VR templates; ' + name + ' requires runtime="gvisor"')
    return name


def inputs(root, name):
    """Stage the installation's pinned inputs on the worker (downloads are cached and verified)."""
    from ....bootstrap import build_input
    from ....vr_installation import prepare_inputs
    staging = Path(root) / 'tools/modal-build' / name.replace('/', '-')
    if staging.exists():
        shutil.rmtree(staging)
    (staging / 'input').mkdir(parents=True)
    downloads = Path(root) / 'downloads'
    downloads.mkdir(exist_ok=True)
    prepare_inputs(SimpleNamespace(downloads=downloads), staging, name)
    shutil.copy2(build_input('xvnc-fast-io.c'), staging / 'input/xvnc-fast-io.c')
    shutil.copy2(build_input('install-sandbox-base.sh'), staging / 'install-sandbox-base.sh')
    for source in ENGINE_GUEST:
        shutil.copy2(build_input(source), staging / source)
    return staging


def definition(recipe, root):
    import modal
    from ... import wire
    name = profile(recipe)
    staging = inputs(root, name)
    here = Path(__file__).resolve().parent
    image = (modal.Image.from_registry(UBUNTU)
             .add_local_dir(staging / 'input', '/sandweave-input', copy=True)
             .add_local_file(staging / 'install-sandbox-base.sh', '/sandweave-build/install.sh', copy=True)
             .run_commands(
                 # The engine's export step (run with python3) has no equivalent here:
                 # the image itself is the result.
                 "printf 'pass\\n' > /usr/local/bin/engine-build-artifacts",
                 'bash /sandweave-build/install.sh ' + name,
                 'mkdir -p /opt/engine-gpu && cp -a /sandweave-output/gpu/. /opt/engine-gpu/',
                 'rm -rf /sandweave-input /sandweave-output /sandweave-build /usr/local/bin/engine-build-artifacts')
             .apt_install('xauth', 'x11-utils', 'dbus')
             # Ubuntu 22.04 does not package python-zstandard; the guest stream compresses frames.
             .run_commands('python3 -m pip install --no-cache-dir zstandard==0.23.0'))
    for source, target in ENGINE_GUEST.items():
        image = image.add_local_file(staging / source, target, copy=True)
    image = image.add_local_file(Path(wire.__file__), '/opt/sandweave/sandweave_wire.py', copy=True)
    for source, target in GUEST.items():
        image = image.add_local_file(here / source, target, copy=True)
    return image.run_commands('chmod 755 ' + ' '.join(ENGINE_GUEST.values()))


def prepare(recipe, root, app):
    """Build (or reuse Modal's cached build of) the template image; returns the built image."""
    image = definition(recipe, root).build(app)
    record = Path(root) / 'tools/modal-images.json'
    known = json.loads(record.read_text()) if record.is_file() else {}
    known[profile(recipe)] = image.object_id
    record.write_text(json.dumps(known, indent=2) + '\n')
    return image

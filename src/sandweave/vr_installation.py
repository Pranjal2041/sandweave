"""Pinned VR inputs for the guest image builder."""
from pathlib import Path
import shutil
import zipfile

from .bootstrap import build_input, download, extract_source
from .sandbox import workspace
from .setup_progress import Stage

GUNSPINNING_SHA256 = '85c440f22f16fcbeec018b0f4be4df8bb3b3ac091a3eda83f43cf2c8d06ad392'


def extract_zip(archive, destination):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with Stage('Extract ' + Path(archive).name, unit='files', detail='Reading archive') as progress, zipfile.ZipFile(archive) as source:
        members = source.infolist()
        progress.update(total=len(members))
        for member in members:
            progress.update(detail=member.filename)
            relative = Path(member.filename)
            mode = member.external_attr >> 16
            if relative.is_absolute() or '..' in relative.parts or (mode & 0o170000) == 0o120000:
                raise ValueError('Archive contains an unsafe path: ' + member.filename)
            target = destination / relative
            if not target.resolve().is_relative_to(destination):
                raise ValueError('Archive traverses a link outside its directory: ' + member.filename)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(member) as stream, target.open('wb') as output:
                    shutil.copyfileobj(stream, output)
                target.chmod(0o755 if mode & 0o111 else 0o644)
            progress.update(advance=1)


def prepare_inputs(builder, root, profile):
    payload = root / 'input'
    monado = 'f8dfadfeaeb46df3eec17bd76b7abdf42a79108c'
    source = download('https://gitlab.freedesktop.org/monado/monado/-/archive/' + monado + '/monado-' + monado + '.tar.gz',
                      builder.downloads, 'monado-' + monado + '.tar.gz')
    extract_source(source, payload / 'monado-source')
    metrics = '41e64fa19837534028c6db89ea641b4cc1552b3c'
    schema = download('https://gitlab.freedesktop.org/monado/utilities/metrics/-/raw/' + metrics + '/proto/monado_metrics.proto',
                      builder.downloads, 'monado_metrics.proto')
    shutil.copy2(schema, payload / schema.name)
    for name in ('monado-vr-lab.patch', 'vr-monado-build.sh', 'vr-monado-guest.sh',
                 'vr-vulkan-xvnc.sh', 'vr-remote-input.py', 'vr_input.py', 'vr_stream_guest.py',
                 'gpu-visual-order.c'):
        shutil.copy2(build_input(name), payload / name)
    shutil.copy2(build_input('install-sandbox-vr.sh'), payload / 'install-vr.sh')
    game_name = 'OpenSaber0.5.0.Linux.x86_64'
    game = download('https://github.com/leandrodreamer/BeepSaber/releases/download/v0.5.0/' + game_name,
                    builder.downloads, game_name,
                    sha256='adc189b96d7321f9c7d142c9243c244394c3ad35a95b8a7fdf0ea4779b74e526')
    shutil.copy2(game, payload / game_name)
    virtualgl = download('https://github.com/VirtualGL/virtualgl/releases/download/3.1.5/virtualgl_3.1.5_amd64.deb',
                         builder.downloads, 'virtualgl_3.1.5_amd64.deb',
                         sha256='df3f7788ce41b182a47c0d298e5cd6d2d63579522cb41825970b7726e825485e')
    shutil.copy2(virtualgl, payload / virtualgl.name)
    if 'gunspinning' not in profile:
        return
    archive = builder.downloads / 'gunspinning-vr-linux.zip'
    if not archive.is_file() or workspace.file_digest(archive) != GUNSPINNING_SHA256:
        raise ValueError('Supply the official GunSpinning Linux 2.0.1 archive with setup --game-archive PATH')
    extract_zip(archive, payload / 'gunspinning-linux-2.0.1')
    (payload / 'gunspinning-linux-2.0.1/GunSpinningVR').chmod(0o755)
    xrizer = download('https://github.com/Supreeeme/xrizer/releases/download/v0.5/xrizer-v0.5.zip',
                      builder.downloads, 'xrizer-v0.5.zip',
                      sha256='935ee21992d5cb99a2bce5b56014cc4d1f981bd9023f0eee65809b251ba7b1ee')
    extract_zip(xrizer, payload / 'xrizer-v0.5')
    primus = '7076c2e6a55cfc7c292eb68ca17b00dae498ef81'
    source = download('https://codeload.github.com/felixdoerre/primus_vk/tar.gz/' + primus,
                      builder.downloads, 'primus-vk-' + primus + '.tar.gz',
                      sha256='23e3c50ed7d65b684a02b8ebe61a82d0749176b09f8e9869c3e3f40ce0ec7ca1')
    extract_source(source, payload / 'gunspinning-primus-source')
    for name in ('primus-vk-visible-rows.patch', 'sdl-gamepad-proxy.c', 'gamepad-input.py'):
        shutil.copy2(build_input(name), payload / name)
    header = download('https://raw.githubusercontent.com/libsdl-org/SDL/release-2.0.8/src/dynapi/SDL_dynapi_procs.h',
                      builder.downloads, 'SDL_dynapi_procs.h',
                      sha256='dbcaad4598fd1977999c157eed098f9b916e5ebf07576e9d57e0d9723228d104')
    shutil.copy2(header, payload / header.name)

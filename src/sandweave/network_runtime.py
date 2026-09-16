"""Install the qualified network helper independently of the guest image."""
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import shutil

from .sandbox import workspace

SOURCE_COMMIT = '4c2bd5720373bf4a6d3e5fc3129cf5ad0dbf8c03'
SOURCE_SHA256 = '0192a6aea35d62b2c262c791a68ac1cbb3ff89d28d64ab405c20c2956b40fbfb'
SOURCE_URL = ('https://salsa.debian.org/sbrivio/passt/-/archive/' + SOURCE_COMMIT +
              '/passt-' + SOURCE_COMMIT + '.tar.gz')


@lru_cache(maxsize=1)
def revision():
    from .bootstrap import build_input
    return hashlib.sha256((SOURCE_SHA256 + workspace.file_digest(
        build_input('passt-backpressure.patch'))).encode()).hexdigest()


def available(root):
    """Cheap presence check; installation verifies the immutable file hashes."""
    directory = Path(root) / 'tools/network'
    try:
        info = json.loads((directory / 'manifest.json').read_text())
        return (info.get('revision') == revision() and
                (directory / 'passt').is_file() and os.access(directory / 'passt', os.X_OK))
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def install(builder, root, *, engine=None, from_source=False):
    from .bootstrap import BUILDER, build_input, download, extract_source
    root = Path(root)
    if available(root):
        return
    if engine is None and not from_source:
        from . import releases
        engine = releases.install(builder.directory, releases.host_info())
    target = root / 'tools/network'
    if engine is not None and available(engine):
        if target.exists():
            shutil.rmtree(target)
        workspace.stage_tree(Path(engine) / 'tools/network', target)
        return

    archive = download(SOURCE_URL, builder.downloads, 'passt-' + SOURCE_COMMIT + '.tar.gz',
                       sha256=SOURCE_SHA256)
    source = root / 'build-tmp/passt-source'
    extract_source(archive, source)
    shutil.copy2(build_input('passt-backpressure.patch'), source / 'sandweave.patch')
    shutil.copy2(build_input('passt-backpressure-test.c'), source / 'regression.c')
    shutil.copy2(build_input('passt-retransmission-test.c'), source / 'retransmission.c')
    image = builder.downloads / 'gvisor-builder.sif'
    builder.pull(BUILDER, image)
    script = '''set -eu
cd /lab/build-tmp/passt-source
git apply sandweave.patch
cat >> Makefile <<'MAKE'
regression: seccomp.h
\t$(CC) $(FLAGS) -Dmain=passt_main -c passt.c -o passt-main.o
\t$(CC) $(FLAGS) -I. regression.c $(filter-out passt.c tcp_buf.c,$(PASST_SRCS)) passt-main.o -Wl,--wrap=tap_send_frames,--wrap=tcp_set_peek_offset,--wrap=tcp_rst_do,--wrap=conn_flag_do,--wrap=recvmsg -o regression
retransmission: seccomp.h
\t$(CC) $(FLAGS) -Dmain=passt_main -c passt.c -o retransmission-main.o
\t$(CC) $(FLAGS) -I. retransmission.c $(filter-out passt.c tcp.c,$(PASST_SRCS)) retransmission-main.o -Wl,--wrap=tcp_buf_send_flag -o retransmission
MAKE
make -j4 VERSION=2025_12_10.d04c480-sandweave2 passt regression retransmission
./regression
./retransmission
'''
    builder.run(builder.container(root, image, 'sh', '-c', script), label='Build network helper')
    # Keep the corresponding source, patch and build instructions with the GPL
    # binary. It links dynamically to libc supplied by our Debian host image.
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for name in ('passt', 'regression.c', 'retransmission.c', 'sandweave.patch'):
        shutil.copy2(source / name, target / name)
    shutil.copytree(source / 'LICENSES', target / 'LICENSES')
    shutil.copy2(archive, target / 'upstream.tar.gz')
    (target / 'build.sh').write_text(script)
    workspace.atomic_json(target / 'manifest.json', {
        'revision': revision(), 'source_commit': SOURCE_COMMIT, 'source_sha256': SOURCE_SHA256,
        'sha256': workspace.file_digest(target / 'passt'),
        'builder_sha256': workspace.file_digest(image)})

#!/usr/bin/env python3
"""Build an explicitly diagnostic nested-gVisor engine on a Linux CPU worker.

Keeps the source and Bazel cache for subsequent investigation. Use --reuse-source
to build an already edited tree. Input patches apply only at initialization.
Optional diagnostic instrumentation is never part of a runtime release.
"""
import argparse
from pathlib import Path
import shutil
import subprocess

from sandweave.bootstrap import Builder, GVISOR_BASE, extract_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--cache', required=True, type=Path)
    parser.add_argument('--engine-patch', required=True, type=Path)
    parser.add_argument('--diagnostic-patch', type=Path)
    parser.add_argument('--reuse-source', action='store_true')
    parser.add_argument('--test', action='store_true', help='run targeted engine regression tests on the CPU host')
    args = parser.parse_args()
    root = args.directory.resolve()
    root.mkdir(parents=True, exist_ok=True)
    source = root / 'sources/gvisor'
    if args.reuse_source:
        if not source.is_dir():
            parser.error('--reuse-source requires an existing source tree')
        print('Building the explicitly reused source tree: ' + str(source), flush=True)
    elif source.exists():
        parser.error('source already exists; use --reuse-source to build that staged tree')
    else:
        extract_source(args.cache / f'gvisor-{GVISOR_BASE}.tar.gz', source)
        subprocess.run(['git', 'apply', str(args.engine_patch.resolve())], cwd=source, check=True)
        if args.diagnostic_patch:
            subprocess.run(['git', 'apply', str(args.diagnostic_patch.resolve())], cwd=source, check=True)
    builder = Builder(root)
    script = '''
cd /lab/sources/gvisor
bazel --batch --output_user_root=/lab/build-tmp/bazel build --jobs=16 \
  //runsc:runsc //runsc/cmd/sentry:gvisor_sentry
mkdir -p /lab/artifacts/gvisor-bin
cp bazel-bin/runsc/runsc_/runsc /lab/artifacts/runsc.new
mv /lab/artifacts/runsc.new /lab/artifacts/runsc
cp bazel-bin/runsc/cmd/sentry/gvisor_sentry_/gvisor_sentry /lab/artifacts/gvisor-bin/gvisor_sentry.new
mv /lab/artifacts/gvisor-bin/gvisor_sentry.new /lab/artifacts/gvisor-bin/gvisor_sentry
'''
    if args.test:
        script += '''
bazel --batch --output_user_root=/lab/build-tmp/bazel test --jobs=16 --test_output=errors \
  //pkg/sentry/pgalloc:pgalloc_test //pkg/sentry/platform/systrap:systrap_test
bazel --batch --output_user_root=/lab/build-tmp/bazel test --jobs=16 --test_output=errors \
  //runsc/container:container_test --test_arg=-test.run=^TestReadonlyBindHintDoesNotCreateFilestore$
'''
    command = builder.container(root, args.cache / 'gvisor-builder.sif', 'sh', '-ec', script)
    subprocess.run(command, check=True, env=builder.env)
    if not args.reuse_source:
        shutil.copy2(args.engine_patch, root / 'engine.patch')
        if args.diagnostic_patch:
            shutil.copy2(args.diagnostic_patch, root / 'diagnostic.patch')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Measure filesystem export and restore in a disposable prepared SDK worker."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

from sandweave import CPU, Memory, Sandbox
from sandweave.sandbox import workspace
from sandweave.sandbox.targets import local_connection


def capacity(path, growth):
    disk = shutil.disk_usage(path)
    if disk.free - growth < disk.total * .15:
        raise RuntimeError('snapshot probe would leave less than 15% free')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mib', type=int, default=3072)
    parser.add_argument('--files', type=int, default=20000)
    parser.add_argument('--storage', choices=['disk', 'memory'], default='disk')
    parser.add_argument('--profile', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    capacity(args.output, 8 * (args.mib * 1024**2 + args.files * 16384) + 4 * 1024**3)
    report = {'mib': args.mib, 'files': args.files, 'storage': args.storage}
    try:
        with Sandbox(cpu=CPU(vcpus=4), memory=Memory('6GiB', '2GiB'),
                     storage=args.storage, profiling=args.profile) as env:
            root = workspace.prepare()
            sys.path.insert(0, str(root / 'scripts'))
            from environment import EnvironmentManager
            manager = EnvironmentManager(root)
            print('Preparing payload', flush=True)
            payload = f'''import os, pathlib
p=pathlib.Path('/workspace/payload'); p.mkdir()
b=bytes(range(256))*4096
with (p/'large').open('wb') as f:
    for _ in range({args.mib}): f.write(b)
for i in range({args.files}):
    d=p/str(i//1000); d.mkdir(exist_ok=True)
    (d/str(i)).write_bytes(b[:8192])
os.link(p/'large', p/'hardlink')
os.setxattr(p/'large','user.binary',b'\\x00\\xff')
os.chmod(p/'large',0o640)
'''
            env.run(argv=['python3', '-c', payload], timeout=300, check=True)
            expected = env.run('sha256sum /workspace/payload/large', timeout=90, check=True).stdout.split()[0]
            profiler = None
            if args.profile:
                target = manager.local / 'export-cpu.pprof'
                profiler = subprocess.Popen([*manager._command(env.id), 'profile', 'cpu',
                    '--duration=30s', '--output=/local/export-cpu.pprof', env.id],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(1)
            print('Capturing', flush=True)
            started = time.perf_counter()
            saved = env.snapshot(state='filesystem')
            report['snapshot_seconds'] = time.perf_counter() - started
            manifest = json.loads((Path(saved.location) / 'snapshot-manifest.json').read_text())
            report['export_seconds'] = manifest['filesystem']['export_seconds']
            report['pause_seconds'] = manifest['pause_seconds']
            report['bytes'] = manifest['files']['rootfs-upper.tar']['size']
            report['timings'] = json.loads((Path(saved.location) / 'save-timings.json').read_text())
            if profiler:
                profiler.wait(timeout=40)
                shutil.copy2(target, args.output / 'export-cpu.pprof')
            print(json.dumps(report), flush=True)
        capacity(args.output, 4 * (args.mib * 1024**2 + args.files * 16384))
        started = time.perf_counter()
        with Sandbox(snapshot=saved) as restored:
            report['restore_seconds'] = time.perf_counter() - started
            assert restored.run('sha256sum /workspace/payload/large', timeout=90, check=True).stdout.split()[0] == expected
            restored.run("python3 -c \"import os; a=os.stat('/workspace/payload/large'); "
                         "assert a.st_ino==os.stat('/workspace/payload/hardlink').st_ino; "
                         "assert a.st_mode & 511 == 416; "
                         "assert os.getxattr('/workspace/payload/large','user.binary')==b'\\x00\\xff'\"", check=True)
            restored.run(argv=['python3', '-c', f'''from pathlib import Path
p=Path('/workspace/payload'); expected=bytes(range(256))*32
assert sum(1 for d in p.iterdir() if d.is_dir() for _ in d.iterdir())=={args.files}
assert all((p/str(i//1000)/str(i)).read_bytes()==expected for i in range({args.files}))
'''], timeout=300, check=True)
        report['verified'] = True
        (args.output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2), flush=True)
    finally:
        c = local_connection()
        try:
            c.call('_shutdown_if_idle')
        finally:
            c.close()


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Qualify a runtime from a fresh installed wheel, outside the source checkout.

--release serves candidate assets locally before publication. Only the private
test wheel's release pin is temporarily replaced; archive bytes are unchanged.
Omit it to exercise the shipped GitHub URLs and checksums without modifications.
"""
import argparse
from contextlib import contextmanager
import functools
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time


@contextmanager
def candidate_release(source, directory):
    from sandweave import releases
    if source is None:
        yield
        return
    original = releases.PIN.read_bytes()
    def write_pin(data):
        # uv may hardlink installed files to its cache or another environment.
        # Replace this test installation's directory entry, never those bytes.
        fd, name = tempfile.mkstemp(prefix='.release-pin-', dir=releases.PIN.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
            os.replace(name, releases.PIN)
        finally:
            Path(name).unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix='release-server-', dir=directory) as temporary:
        web = Path(temporary)
        manifest = json.loads((source / 'manifest.json').read_text())
        for artifact in manifest['artifacts']:
            shutil.copyfile(source / artifact['name'], web / artifact['name'])
        handler = functools.partial(SimpleHTTPRequestHandler, directory=str(web))
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}/'
        for artifact in manifest['artifacts']:
            artifact['url'] = base + artifact['name']
        data = json.dumps(manifest).encode()
        (web / 'manifest.json').write_bytes(data)
        try:
            write_pin(json.dumps({'schema_version': 1, 'manifest': {
                'url': base + 'manifest.json', 'sha256': hashlib.sha256(data).hexdigest()}}).encode())
            yield
        finally:
            write_pin(original)
            server.shutdown()
            server.server_close()
            thread.join()


def acceptance(args, report):
    from sandweave import Sandbox, releases
    from sandweave.sandbox import workspace
    from sandweave.sandbox.process import Process
    from sandweave.sandbox.targets import local_connection
    assert not args.directory.exists(), 'Use a new empty destination for first-install acceptance'
    assert 'SANDWEAVE_ASSETS' not in os.environ
    os.environ['SANDWEAVE_HOME'] = str(args.directory)
    args.directory.mkdir(parents=True)
    pointer = workspace.default_home() / 'location.json'
    before = pointer.read_bytes() if pointer.exists() else None
    report.update(host=platform.node(), kernel=platform.release(), architecture=platform.machine(),
                  python=sys.version, sdk=__import__('importlib.metadata', fromlist=['version']).version('sandweave'),
                  distribution='candidate-local-http' if args.release else 'published-github',
                  installation='automatic-first-use' if args.automatic else 'cli-setup')
    started = time.monotonic()
    with candidate_release(args.release, args.directory):
        if args.automatic:
            with Sandbox() as initial:
                assert initial.run("python -c 'print(2 + 2)'").stdout == '4\n'
        else:
            subprocess.run([str(Path(sys.executable).parent / 'sandweave'), 'setup', '--yes', '--template', 'coding',
                            '--directory', str(args.directory)], check=True)
    report['first_setup_seconds'] = time.monotonic() - started
    assert (pointer.read_bytes() if pointer.exists() else None) == before, 'Explicit storage changed global location'
    root = workspace.assets()
    report['engine'] = releases.validate_engine(root)
    assert not (root / 'tools/gvisor-builder.sif').exists()
    assert not list((args.directory / 'downloads').glob('*builder*'))
    assert not list((args.directory / 'logs/setup').glob('Build gVisor*'))
    report['compiler_downloaded_or_run'] = False
    started = time.monotonic()
    with Sandbox(cpu=2, memory='512MiB') as env:
        report['ready_seconds'] = env.timings['ready_seconds']
        assert env.run("python -c 'import os; print(os.cpu_count())'").stdout.strip() == '2'
        assert env.run('test ! -e /dev/kvm').returncode == 0
        assert env.run('curl -fsS --max-time 20 -o /dev/null -w "%{http_code}" https://example.com', timeout=25).stdout == '200'
        env.files.write_text('/workspace/value', 'saved files')
        env.pause()
        assert env.status()['state'] == 'paused'
        env.resume()
        assert env.run('echo resumed').stdout == 'resumed\n'
        cache = env.cache('release-acceptance')
        assert cache.verify()['status'] == 'passed'
        env.files.write_text('/workspace/value', 'source changed')
        with Sandbox(cache=cache) as clone:
            assert clone.files.read_text('/workspace/value') == 'saved files'
        process = env.exec(argv=['python', '-u', '-c',
            'import uuid; secret=uuid.uuid4().hex; print("ready"); print(secret + ":" + input())'])
        assert process.stdout.readline() == 'ready\n'
        snapshot = env.snapshot(state='memory')
        assert snapshot.verify()['status'] == 'passed'
        process.stdin.write('source\n')
        source = process.stdout.readline().strip()
        with Sandbox(snapshot=snapshot) as clone:
            restored = Process(clone, process.id)
            assert restored.stdout.readline() == 'ready\n'
            restored.stdin.write('clone\n')
            copied = restored.stdout.readline().strip()
            assert copied.split(':')[0] == source.split(':')[0]
            assert copied.endswith(':clone') and source.endswith(':source')
            restored.stdin.close()
            assert restored.wait(timeout=5) == 0
        process.stdin.close()
        assert process.wait(timeout=5) == 0
    with Sandbox(network='offline') as env:
        result = env.run("python -c 'import socket; socket.create_connection((\"1.1.1.1\",443),timeout=1)'", check=False)
        assert result.returncode != 0
    report['lifecycle_seconds'] = time.monotonic() - started
    connection = local_connection()
    connection.call('_shutdown_if_idle')
    connection.close()
    report['checks'] = ['first-install', 'exact-engine-hashes', 'no-compiler', 'cpu-override', 'no-kvm',
                        'internet', 'files', 'pause-resume', 'filesystem-cache', 'process-memory-restore',
                        'offline-network', 'context-cleanup', 'global-location-preserved']
    report['status'] = 'passed'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=lambda x: Path(x).resolve())
    parser.add_argument('--release', type=lambda x: Path(x).resolve())
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--automatic', action='store_true', help='install through Sandbox() instead of setup')
    args = parser.parse_args()
    report = {'status': 'failed'}
    try:
        acceptance(args, report)
    finally:
        args.report.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2), flush=True)

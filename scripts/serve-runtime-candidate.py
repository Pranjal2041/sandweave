#!/usr/bin/env python3
"""Serve a candidate on localhost and pin this isolated SDK environment to it."""
import argparse
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile

from sandweave import releases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('port', type=int)
    args = parser.parse_args()
    source = args.source.resolve()
    manifest = json.loads((source / 'manifest.json').read_text())
    base = f'http://127.0.0.1:{args.port}/'
    for artifact in manifest['artifacts']:
        artifact['url'] = base + artifact['name']
    data = json.dumps(manifest).encode()
    (source / 'served-manifest.json').write_bytes(data)
    pin = json.dumps({'schema_version': 1, 'manifest': {
        'url': base + 'served-manifest.json', 'sha256': hashlib.sha256(data).hexdigest()}}).encode()
    with ThreadingHTTPServer(('127.0.0.1', args.port),
                             partial(SimpleHTTPRequestHandler, directory=str(source))) as server:
        fd, name = tempfile.mkstemp(prefix='.release-pin-', dir=releases.PIN.parent)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(pin)
        os.chmod(name, 0o644)
        os.replace(name, releases.PIN)
        (source / 'serve.pid').write_text(str(os.getpid()) + '\n')
        print('pinned', releases.PIN, 'to', base, flush=True)
        server.serve_forever()


if __name__ == '__main__':
    main()

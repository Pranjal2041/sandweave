#!/usr/bin/env python3
"""Transfer a SHA256SUMS directory through bounded Modal filesystem requests."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import PurePosixPath

import modal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source_sandbox')
    parser.add_argument('destination_sandbox')
    parser.add_argument('source_directory')
    parser.add_argument('destination_directory')
    args = parser.parse_args()
    source = modal.Sandbox.from_id(args.source_sandbox)
    destination = modal.Sandbox.from_id(args.destination_sandbox)
    manifest = source.filesystem.read_bytes(args.source_directory + '/SHA256SUMS')
    entries = []
    for line in manifest.decode().splitlines():
        digest, name = line.split(maxsplit=1)
        name = name.lstrip('*')
        if PurePosixPath(name).name != name or not name.startswith('chunk-'):
            raise ValueError('invalid chunk name')
        entries.append((digest, name))

    def copy(entry):
        digest, name = entry
        data = source.filesystem.read_bytes(args.source_directory + '/' + name)
        if len(data) > 32 * 1024**2 or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('invalid source chunk: ' + name)
        path = args.destination_directory + '/' + name
        destination.filesystem.write_bytes(data, path)
        actual = destination.exec('sha256sum', path).stdout.read().split()[0]
        if actual != digest:
            raise ValueError('destination checksum mismatch: ' + name)
        return name

    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = [pool.submit(copy, entry) for entry in entries]
        for count, done in enumerate(as_completed(pending), 1):
            print(json.dumps({'copied': done.result(), 'completed': count, 'total': len(entries)}), flush=True)
    destination.filesystem.write_bytes(manifest, args.destination_directory + '/SHA256SUMS')


if __name__ == '__main__':
    main()

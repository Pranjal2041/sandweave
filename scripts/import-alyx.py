#!/usr/bin/env python3
"""Copy an existing Alyx installation through ut's streaming file service.

The input is a JSON array of {path: relative/path, size: bytes} records.
Unlike ut cp, this keeps large files out of memory and resumes partial files.
It reads the source only; no source-machine archive or extra disk space is needed.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def main():
    os.umask(0o022)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--machine', default='pranjala-win')
    parser.add_argument('--source', default='C:/Program Files (x86)/Steam/steamapps/common/Half-Life Alyx')
    parser.add_argument('--destination', type=Path, default=Path('tools/gpu/alyx'))
    parser.add_argument('--broker-port', type=int, default=8722)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--match', default='.*', help='regular expression selecting relative paths')
    parser.add_argument('--priority', default=r'^game/(bin|hlvr/bin)/',
                        help='copy engine modules first so startup can be checked during transfer')
    parser.add_argument('--largest-first', action='store_true',
                        help='within each priority group, start large files first to reduce the transfer tail')
    args = parser.parse_args()
    if not 1 <= args.workers <= 8 or not 1 <= args.broker_port <= 65535:
        parser.error('invalid worker count or local broker port')
    entries = json.loads(args.manifest.read_text())
    selected, seen = [], set()
    for entry in entries:
        path = PurePosixPath(entry['path'])
        if (path.is_absolute() or '..' in path.parts or ':' in str(path)
                or '\\' in str(path) or str(path) in ('', '.') or str(path) in seen
                or not isinstance(entry['size'], int) or entry['size'] < 0):
            parser.error('invalid or duplicate manifest entry')
        seen.add(str(path))
        if re.search(args.match, str(path)):
            selected.append(entry)
    # Native modules retain priority; size order can favor startup or bulk completion.
    selected.sort(key=lambda item: (not bool(re.search(args.priority, item['path'])),
                                   -item['size'] if args.largest_first else item['size']))
    args.destination.mkdir(parents=True, exist_ok=True)
    destination = args.destination.resolve()
    destination.chmod(0o755)
    lock = os.open(destination / '.import.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error('another importer is writing this destination')

    def copy(entry):
        target = destination / entry['path']
        if not target.resolve().is_relative_to(destination):
            raise ValueError('destination escapes through a symlink')
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.stat().st_size != entry['size']:
                raise ValueError('existing file has a different size: '+entry['path'])
            return 0
        partial = target.with_name(target.name+'.partial')
        if partial.is_symlink():
            raise ValueError('partial file is a symlink')
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > entry['size']:
            raise ValueError('partial file exceeds source size')
        original = offset
        query = urlencode({'_mhost': args.machine, '_mpath': '/fs/read',
                           'path': args.source.rstrip('/')+'/'+entry['path']})
        url = f'http://127.0.0.1:{args.broker_port}/mesh/proxy?'+query
        with partial.open('ab') as output:
            while offset < entry['size']:
                end = min(offset+8*1024*1024, entry['size'])-1
                for attempt in range(4):
                    try:
                        request = Request(url, headers={'Range': f'bytes={offset}-{end}'})
                        with urlopen(request, timeout=60) as response:
                            expected = f'bytes {offset}-{end}/{entry["size"]}'
                            if response.status != 206 or response.headers.get('Content-Range') != expected:
                                raise ValueError('source changed or broker did not preserve byte ranges')
                            data = response.read(end-offset+2)
                            if len(data) != end-offset+1:
                                raise OSError('incomplete file range')
                        output.write(data)
                        offset = end+1
                        break
                    except OSError:
                        if attempt == 3:
                            raise
                        time.sleep(attempt+1)
        with partial.open('rb') as stream:
            executable = stream.read(4) == b'\x7fELF'
        partial.chmod(0o755 if executable or target.suffix == '.sh' else 0o644)
        partial.replace(target)
        return offset-original

    start, transferred, completed, completed_bytes = time.monotonic(), 0, 0, 0
    total = sum(entry['size'] for entry in selected)
    print(json.dumps({'selected_files': len(selected), 'selected_bytes': total}), flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(copy, entry): entry for entry in selected}
        last_report = start
        for future in as_completed(futures):
            try:
                transferred += future.result()
            except Exception:
                for pending in futures:
                    pending.cancel()
                raise
            completed += 1
            completed_bytes += futures[future]['size']
            now = time.monotonic()
            if now-last_report >= 10 or completed == len(selected):
                print(json.dumps({'completed_files': completed, 'total_files': len(selected),
                                  'completed_bytes': completed_bytes, 'total_bytes': total,
                                  'transferred_bytes': transferred,
                                  'elapsed_seconds': round(now-start, 1)}), flush=True)
                last_report = now


if __name__ == '__main__':
    main()

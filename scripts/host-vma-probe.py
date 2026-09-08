#!/usr/bin/env python3
"""Sample mapping counts for one owned process tree without modifying it."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('pid', type=int)
parser.add_argument('--seconds', type=float, default=50)
parser.add_argument('--interval', type=float, default=.5)
args = parser.parse_args()
if args.pid <= 1 or not 0 < args.seconds <= 300 or not .1 <= args.interval <= 10:
    parser.error('use an owned PID > 1, duration (0,300], and interval [0.1,10]')
root = Path('/proc')/str(args.pid)
if root.stat().st_uid != os.getuid():
    parser.error('the root process must belong to the current user')


def start_time(path):
    fields = path.joinpath('stat').read_text().split(') ', 1)[1].split()
    return None if fields[0] == 'Z' else fields[19]


original = start_time(root)
deadline = time.monotonic()+args.seconds
while time.monotonic() < deadline:
    try:
        if original is None or start_time(root) != original:
            break
    except FileNotFoundError:
        break
    pairs = [tuple(map(int, line.split())) for line in subprocess.check_output(
        ['ps', '-e', '-o', 'pid=,ppid='], text=True).splitlines()]
    selected = {args.pid}
    while True:
        added = {pid for pid, parent in pairs if parent in selected}-selected
        if not added:
            break
        selected.update(added)
    counts = []
    for pid in selected:
        path = Path('/proc')/str(pid)
        try:
            count = len(path.joinpath('maps').read_text().splitlines())
            counts.append({'pid': pid, 'maps': count,
                           'comm': path.joinpath('comm').read_text().strip()})
        except (FileNotFoundError, ProcessLookupError):
            pass
    print(json.dumps({'time': time.time(), 'root_pid': args.pid,
                      'processes': sorted(counts, key=lambda item: -item['maps'])[:8]}), flush=True)
    time.sleep(min(args.interval, max(0, deadline-time.monotonic())))

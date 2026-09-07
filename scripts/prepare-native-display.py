#!/usr/bin/env python3
"""Quarantine only stale files for the lab's private native X display."""
import datetime
import os
from pathlib import Path
import socket
import subprocess

root = Path(__file__).resolve().parent.parent
processes = subprocess.check_output(['ps', '-u', str(os.getuid()), '-o', 'args='], text=True)
if any(line.startswith('Xtigervnc :98 ') for line in processes.splitlines()):
    raise SystemExit('The native lab X server is still running.')
for port in (25902, 28080):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', port))
stale = [root / 'runs/native-control/tmp/.X98-lock',
         root / 'runs/native-control/tmp/.X11-unix/X98']
present = [p for p in stale if p.exists() or p.is_symlink()]
if present:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    target = root / 'runs/native-control' / f'stale-x11-{stamp}'
    target.mkdir()
    for source in present:
        source.rename(target / source.name)
    print(f'Quarantined stale native display files in {target}')

#!/usr/bin/env python3
"""Check unprivileged writable native overlays without changing the source image."""
import json
from pathlib import Path
import subprocess
import tempfile

with tempfile.TemporaryDirectory(prefix='sandweave-native-probe-') as temporary:
    root = Path(temporary)
    (root / 'overlay/upper').mkdir(parents=True)
    (root / 'overlay/work').mkdir()
    command = ['apptainer', 'exec', '--userns', '--fakeroot', '--containall', '--cleanenv', '--no-home',
               '--overlay', str(root / 'overlay'),
               '/data/user_data/pranjala/general-vm/tools/gvisor-builder.sif']
    first = subprocess.run([*command, 'python3', '-c',
        'import os,pathlib,subprocess; assert not pathlib.Path("/dev/kvm").exists(); '
        'pathlib.Path("/opt/native-overlay-test").write_text("persistent"); '
        'print(os.getuid()); print(subprocess.check_output(["id"]).decode())'], capture_output=True, text=True)
    print(json.dumps({'first': {'returncode': first.returncode, 'stdout': first.stdout, 'stderr': first.stderr}}), flush=True)
    if first.returncode:
        raise SystemExit(first.returncode)
    second = subprocess.run([*command, 'cat', '/opt/native-overlay-test'], capture_output=True, text=True)
    print(json.dumps({'second': {'returncode': second.returncode, 'stdout': second.stdout, 'stderr': second.stderr}}), flush=True)
    assert second.returncode == 0 and second.stdout == 'persistent'

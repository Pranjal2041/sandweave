#!/usr/bin/env python3
"""Start a private native Apptainer graphics control using the staged GPU stack."""
import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess

import gvisor_gpu

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--gpu', type=int, default=0)
parser.add_argument('name')
parser.add_argument('command', nargs=argparse.REMAINDER)
args = parser.parse_args()
if not re.fullmatch(r'[a-zA-Z0-9_-]+', args.name):
    parser.error('invalid session name')
command = args.command
if command and command[0] == '--':
    command = command[1:]
if not command:
    parser.error('an application command is required')
devices = gvisor_gpu.allocated_device(args.gpu)
identity = gvisor_gpu.device_identity(args.gpu)
lab = Path(__file__).resolve().parent.parent
local = Path((lab / 'runs/local-path.txt').read_text().strip())
session = local / 'native-gpu' / args.name
session.mkdir(parents=True, exist_ok=False)
for name in ('home', 'tmp', 'runtime', 'config', 'cache'):
    (session / name).mkdir(mode=0o700)
with socket.socket() as reservation:
    reservation.bind(('127.0.0.1', 0))
    port = reservation.getsockname()[1]
binds = []
for device in devices:
    binds += ['--bind', device['path'] + ':' + device['path']]
startup = '''set -eu
test ! -e /dev/kvm
export HOME=/session/home DISPLAY=:97 XDG_RUNTIME_DIR=/session/runtime
export XDG_CONFIG_HOME=/session/config XDG_CACHE_HOME=/session/cache
printf 'labvnc01\\n' | vncpasswd -f > /session/vnc.passwd
chmod 600 /session/vnc.passwd
Xtigervnc :97 -geometry 1280x800 -depth 24 -localhost yes -rfbport "$1" \\
  -SecurityTypes VncAuth -rfbauth /session/vnc.passwd -ac -nolisten tcp \\
  > /session/xvnc.log 2>&1 &
xpid=$!
shift
trap 'kill "$xpid" "${wm:-$xpid}" 2>/dev/null || true' EXIT
for attempt in $(seq 1 30); do
    if xdpyinfo >/dev/null 2>&1; then break; fi
    kill -0 "$xpid"
    sleep 1
done
xdpyinfo >/dev/null
metacity --no-composite > /session/wm.log 2>&1 &
wm=$!
exec_app() { /lab/scripts/gvisor-guest-gpu.sh "$@" > /session/app.log 2>&1; }
exec_app "$@"
'''
launch = ['apptainer', 'exec', '--userns', '--containall', '--cleanenv', '--no-home',
          *binds, '--bind', f'{lab}:/lab:ro', '--bind', f'{session}:/session',
          '--bind', f'{session}/tmp:/tmp', '--bind', f'{session}/home:/home/{os.environ["USER"]}',
          '--bind', f'{lab}/tools/gpu:/opt/engine-gpu:ro', '--pwd', '/session',
          str(local / 'native-root'), 'bash', '-c', startup, 'native-gpu', str(port), *command]
with (session / 'launcher.log').open('wb') as output:
    child = subprocess.Popen(launch, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
metadata = {'pid': child.pid, 'vnc_port': port, 'session': str(session), 'gpu': identity,
            'command': command}
(lab / 'runs' / (args.name + '.json')).write_text(json.dumps(metadata, indent=2) + '\n')
print(json.dumps(metadata))

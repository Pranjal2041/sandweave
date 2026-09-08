#!/usr/bin/env python3
"""Operate a separate experimental headless GNOME Wayland desktop in a VR lab sandbox."""
import argparse
import json
from pathlib import Path
import subprocess
import time
import uuid

from environment import EnvironmentManager

UNIT = 'vr-wayland-gnome'
RUNTIME = '/run/user/1000/wayland-gnome'

INSPECT = r'''
import dbus, json, subprocess
from pathlib import Path
main = int(subprocess.check_output(['systemctl', 'show', 'vr-wayland-gnome', '-p', 'MainPID', '--value']))
if not main:
    print(json.dumps({'status': 'stopped'}))
    raise SystemExit
children = subprocess.check_output(['ps', '-eo', 'pid,ppid,comm'], text=True).splitlines()[1:]
shell = next(int(pid) for pid, parent, comm in map(str.split, children)
             if int(parent) == main and comm == 'gnome-shell')
env = dict(item.split('=', 1) for item in Path(f'/proc/{shell}/environ').read_text().split('\0') if '=' in item)
xpid = next(int(pid) for pid, parent, comm in map(str.split, children)
            if int(parent) == shell and comm == 'Xwayland')
argv = Path(f'/proc/{xpid}/cmdline').read_text().split('\0')
bus = dbus.bus.BusConnection(env['DBUS_SESSION_BUS_ADDRESS'])
ready = all(bus.name_has_owner(name) for name in ('org.gnome.Mutter.RemoteDesktop', 'org.gnome.Shell.Screenshot'))
print(json.dumps({'status': 'running' if ready else 'starting', 'shell_pid': shell, 'xwayland_pid': xpid,
                  'x11_display': argv[1], 'xauthority': argv[argv.index('-auth')+1],
                  'runtime_dir': env['XDG_RUNTIME_DIR'], 'wayland_display': 'wayland-gnome',
                  'dbus_address': env['DBUS_SESSION_BUS_ADDRESS']}))
'''

DISMISS_OVERVIEW = '''import dbus, sys, time
bus = dbus.bus.BusConnection(sys.argv[1])
root = dbus.Interface(bus.get_object('org.gnome.Mutter.RemoteDesktop', '/org/gnome/Mutter/RemoteDesktop'), 'org.gnome.Mutter.RemoteDesktop')
path = root.CreateSession()
session = dbus.Interface(bus.get_object('org.gnome.Mutter.RemoteDesktop', path), 'org.gnome.Mutter.RemoteDesktop.Session')
session.Start()
try:
    session.NotifyKeyboardKeysym(dbus.UInt32(0xff1b), True)
    session.NotifyKeyboardKeysym(dbus.UInt32(0xff1b), False)
    time.sleep(.3)
finally:
    session.Stop()
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name', help='separate prepared sandbox named vr-wayland-*')
    commands = parser.add_subparsers(dest='command', required=True)
    start = commands.add_parser('start')
    start.add_argument('--width', type=int, default=1280)
    start.add_argument('--height', type=int, default=800)
    start.add_argument('--hz', type=int, default=120)
    start.add_argument('--probe-renderer', action='store_true', help='log actual EGL renderer through a diagnostic interposer')
    start.add_argument('--xwayland-renderer', choices=['shm', 'auto'], default='shm',
                       help='shm keeps VirtualGL readback; auto also probes the unqualified EGLStream/GBM path')
    commands.add_parser('status')
    commands.add_parser('stop')
    capture = commands.add_parser('capture')
    capture.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if not args.name.startswith('vr-wayland-'):
        parser.error('use a separate disposable vr-wayland-* sandbox')
    manager = EnvironmentManager()
    status = manager.status(args.name)
    if status['status'] != 'running' or not status.get('gpu'):
        parser.error('sandbox must be running with GPU access')
    prefix = [*manager._command(args.name), 'exec', args.name]

    def guest(*command):
        return manager._run([*prefix, *command])

    if args.command == 'start':
        if not (320 <= args.width <= 4096 and 200 <= args.height <= 4096 and 30 <= args.hz <= 240):
            parser.error('invalid virtual monitor size or refresh rate')
        if guest('systemctl', 'show', UNIT, '-p', 'ActiveState', '--value').strip() in ('active', 'activating', 'deactivating'):
            parser.error('the owned Wayland session is already running')
        guest('test', '!', '-e', '/dev/kvm')
        guest('install', '-d', '-m', '700', '-o', '1000', '-g', '1000', RUNTIME)
        extra = []
        if args.xwayland_renderer == 'shm':
            extra.append('--setenv=XWAYLAND_NO_GLAMOR=1')
        if args.probe_renderer:
            guest('mkdir', '-p', '/opt/vr')
            subprocess.run([*prefix, 'sh', '-c',
                            'umask 022; cat > /opt/vr/egl-renderer-probe.c; '
                            'cc -shared -fPIC -O2 -o /opt/vr/egl-renderer-probe.so /opt/vr/egl-renderer-probe.c -ldl'],
                           input=(manager.lab/'scripts/egl-renderer-probe.c').read_bytes(),
                           capture_output=True, check=True, timeout=30)
            extra.append('--setenv=LD_PRELOAD=/opt/vr/egl-renderer-probe.so')
        guest('systemd-run', '--unit='+UNIT, '--uid=ga', '--collect',
              '--property=TimeoutStopSec=5',
              '--setenv=XDG_RUNTIME_DIR='+RUNTIME, '--setenv=XDG_SESSION_TYPE=wayland',
              '--setenv=HOME=/home/ga', '--setenv=COGL_DRIVER=gles2', *extra, '/usr/local/bin/engine-gpu',
              'dbus-run-session', '--', 'gnome-shell', '--headless', '--wayland',
              f'--virtual-monitor={args.width}x{args.height}@{args.hz}',
              '--wayland-display=wayland-gnome', '--sm-disable')
        deadline = time.monotonic()+60
        while time.monotonic() < deadline:
            try:
                result = json.loads(guest('runuser', '-u', 'ga', '--', 'python3', '-c', INSPECT))
                if result['status'] == 'running':
                    guest('runuser', '-u', 'ga', '--', 'python3', '-c', DISMISS_OVERVIEW, result['dbus_address'])
                    print(json.dumps(result, indent=2))
                    return
            except RuntimeError:
                pass
            time.sleep(.2)
        guest('systemctl', 'stop', UNIT)
        raise RuntimeError('Wayland initialization failed; inspect journalctl -u '+UNIT)
    elif args.command == 'stop':
        if guest('sh', '-c', 'pgrep -x monado-service || true').strip():
            parser.error('close the VR stream or stop its game/runtime first')
        guest('systemctl', 'stop', UNIT)
        print(json.dumps({'status': 'stopped'}))
    else:
        state = json.loads(guest('runuser', '-u', 'ga', '--', 'python3', '-c', INSPECT))
        if args.command == 'status':
            print(json.dumps(state, indent=2))
            return
        if state['status'] != 'running':
            parser.error('Wayland session is not running')
        path = '/tmp/wayland-capture-'+uuid.uuid4().hex+'.png'
        code = '''import dbus, sys
bus = dbus.bus.BusConnection(sys.argv[1])
bus.request_name('org.gnome.Screenshot', dbus.bus.NAME_FLAG_DO_NOT_QUEUE)
obj = bus.get_object('org.gnome.Shell.Screenshot', '/org/gnome/Shell/Screenshot')
ok, path = dbus.Interface(obj, 'org.gnome.Shell.Screenshot').Screenshot(False, False, sys.argv[2])
if not ok: raise RuntimeError('GNOME screenshot failed')
'''
        try:
            guest('runuser', '-u', 'ga', '--', 'python3', '-c', code, state['dbus_address'], path)
            data = subprocess.check_output([*prefix, 'cat', path], timeout=15)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(data)
        finally:
            guest('rm', '-f', path)
        print(args.output)


if __name__ == '__main__':
    main()

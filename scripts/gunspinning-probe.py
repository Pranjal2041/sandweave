#!/usr/bin/env python3
"""Launch the official GunSpinning VR Linux 2.0.1 in a disposable GPU guest.

Requires a prepared Monado guest (prepare-vr-lab.py or a cold snapshot),
xrizer-v0.5 and the unmodified itch.io Linux build staged at
tools/gpu/vr/gunspinning-linux-2.0.1. Gamepad input uses the opt-in SDL proxy.
"""
import argparse
import json
import subprocess

from environment import EnvironmentManager


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('name')
    p.add_argument('command', choices=('prepare', 'start', 'stop', 'log', 'exec'))
    p.add_argument('--mode', choices=('gamepad', 'motion'), default='gamepad')
    p.add_argument('--renderer', choices=('gl', 'vulkan'), default='vulkan')
    p.add_argument('--width', type=int, default=1280)
    p.add_argument('--height', type=int, default=720)
    p.add_argument('--audio', action='store_true', help='enable audio (requires a working guest audio sink)')
    p.add_argument('guest_command', nargs='*')
    args = p.parse_args()
    manager = EnvironmentManager()
    status = manager.status(args.name)
    if not args.name.startswith('vr-') or status['status'] != 'running' or not status.get('gpu'):
        p.error('select a running disposable GPU guest named vr-*')
    prefix = [*manager._command(args.name), 'exec', args.name]

    def guest(*command, payload=None, timeout=45):
        result = subprocess.run([*prefix, *command], input=payload,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if result.returncode:
            raise RuntimeError((result.stdout + result.stderr).decode(errors='replace')[-6000:])
        return result.stdout

    if args.command == 'exec':
        import sys
        sys.stdout.buffer.write(guest(*args.guest_command))
    elif args.command == 'prepare':
        guest('sh', '-c', 'if pgrep -x GunSpinningVR >/dev/null; then echo "Stop GunSpinningVR before preparing this guest" >&2; exit 1; fi')
        guest('mkdir', '-p', '/opt/gunspinning-lab')
        for filename in ('gamepad-input.py',):
            data = (manager.lab / 'scripts' / filename).read_bytes()
            guest('sh', '-c', 'cat > /opt/gunspinning-lab/' + filename, payload=data)
        guest('sh', '-c', 'cat > /opt/vr/vr-vulkan-xvnc.sh',
              payload=(manager.lab / 'scripts/vr-vulkan-xvnc.sh').read_bytes())
        guest('sh', '-c', '''
set -eu
test -x /opt/engine-gpu/vr/gunspinning-linux-2.0.1/GunSpinningVR
test -f /opt/engine-gpu/vr/libsdl-gamepad-proxy.so
test -f /opt/engine-gpu/vr/xrizer-v0.5/bin/linux64/vrclient.so
install -m755 /opt/engine-gpu/vr/libsdl-gamepad-proxy.so /opt/gunspinning-lab/libsdl-gamepad-proxy.so
runuser -u ga -- python3 -c "import ctypes; ctypes.CDLL('/opt/gunspinning-lab/libsdl-gamepad-proxy.so')"
mkdir -p /home/ga/.config/openvr
chown -R ga:ga /home/ga/.config/openvr
''')
        guest('sh', '-c', '''
set -eu
test -f /opt/vr/vr-vulkan-xvnc.sh
test -f /usr/share/vulkan/implicit_layer.d/primus_vk.json
mkdir -p /opt/gunspinning-lab/primus-build
cp -a /opt/engine-gpu/vr/gunspinning-primus-source/. /opt/gunspinning-lab/primus-build/
chmod -R u+w /opt/gunspinning-lab/primus-build
touch /opt/gunspinning-lab/primus-build/primus_vk.cpp
make -C /opt/gunspinning-lab/primus-build -j2 libprimus_vk.so CXXFLAGS=-O2
install -m755 /opt/gunspinning-lab/primus-build/libprimus_vk.so /opt/gunspinning-lab/libprimus_vk.so
''', timeout=180)
        guest('python3', '-c', '''
import json
from pathlib import Path
p = Path('/usr/share/vulkan/implicit_layer.d/primus_vk.json')
backup = Path('/opt/gunspinning-lab/primus-original.json')
if not backup.exists():
    backup.write_bytes(p.read_bytes())
data = json.loads(p.read_text())
data['layer']['library_path'] = '/opt/gunspinning-lab/libprimus_vk.so'
p.write_text(json.dumps(data, indent=2) + '\\n')
''')
        paths = json.dumps(dict(version=1, jsonid='vrpathreg',
                                runtime=['/opt/engine-gpu/vr/xrizer-v0.5'],
                                config=[], log=[], external_drivers=[])).encode()
        guest('runuser', '-u', 'ga', '--', 'sh', '-c',
              'cat > /home/ga/.config/openvr/openvrpaths.vrpath', payload=paths)
        print('Prepared', args.name)
    elif args.command == 'start':
        unit = 'gunspinning-' + args.mode
        common = ['systemd-run', '--unit=' + unit, '--uid=ga', '--collect',
                  '--property=TimeoutStopSec=3',
                  '--setenv=HOME=/home/ga', '--setenv=DISPLAY=:1',
                  '--setenv=XAUTHORITY=/home/ga/.Xauthority',
                  '--setenv=XDG_RUNTIME_DIR=/run/user/1000',
                  '--setenv=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus',
                  '--setenv=XR_RUNTIME_JSON=/opt/vr/monado/share/openxr/1/openxr_monado.json',
                  '--setenv=VR_OVERRIDE=/opt/engine-gpu/vr/xrizer-v0.5',
                  '--setenv=OXR_LAB_ALLOW_MISSING_GLX_CONFIG=1',
                  '--setenv=VK_DRIVER_FILES=/opt/vr/vulkan.json',
                  '--setenv=VK_ICD_FILENAMES=/opt/vr/vulkan.json',
                  '--property=WorkingDirectory=/opt/engine-gpu/vr/gunspinning-linux-2.0.1']
        if args.mode == 'gamepad':
            common += ['--setenv=SDL_DYNAMIC_API=/opt/gunspinning-lab/libsdl-gamepad-proxy.so']
        wrapper = ['/usr/local/bin/engine-gpu-gl'] if args.renderer == 'gl' else ['sh', '/opt/vr/vr-vulkan-xvnc.sh']
        cmd = [*common, *wrapper, '/opt/engine-gpu/vr/gunspinning-linux-2.0.1/GunSpinningVR',
               '-screen-fullscreen', '0', '-screen-width', str(args.width), '-screen-height', str(args.height),
               '-vrmode', 'None' if args.mode == 'gamepad' else 'OpenVR',
               '-force-glcore' if args.renderer == 'gl' else '-force-vulkan',
               '-logFile', '/tmp/gunspinning-' + args.mode + '.log']
        if not args.audio:
            cmd += ['-noaudio']
        guest(*cmd)
        print('Started', unit, '(inspect rendering and controls before claiming acceptance)')
    elif args.command == 'stop':
        guest('systemctl', 'stop', 'gunspinning-' + args.mode)
    elif args.command == 'log':
        import sys
        sys.stdout.buffer.write(guest('tail', '-160', '/tmp/gunspinning-' + args.mode + '.log'))


if __name__ == '__main__':
    main()

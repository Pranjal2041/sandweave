#!/usr/bin/env python3
"""Prepare or run a bounded AMS2 demo startup probe in a disposable VR sandbox."""
import argparse
import json
from pathlib import Path
import subprocess

from environment import EnvironmentManager

PREPARE = r'''
import json, os, shutil, sys
from pathlib import Path, PurePosixPath
root, source = Path('/opt/ams2'), Path('/opt/engine-gpu/racing/ams2-demo')
for entry in json.load(sys.stdin):
    path = PurePosixPath(entry['path'])
    if path.is_absolute() or '..' in path.parts or ':' in str(path) or chr(92) in str(path):
        raise ValueError('invalid installation path')
    target, original = root/path, source/path
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        if target.readlink() != original:
            raise ValueError('unexpected asset link')
    elif not target.exists():
        target.symlink_to(original)
for directory, children, files in os.walk(root):
    os.chown(directory, 1000, 1000)
    os.chmod(directory, 0o755)
'''

VR_FILES = r'''
import json, shutil
from pathlib import Path
base = Path('/opt/engine-gpu/vr/GE-Proton9-27/files/lib64')
prefix = Path('/home/ga/.local/share/ams2-wine/drive_c')
system = prefix/'windows/system32'
for name in ('dxgi.dll','d3d11.dll','d3d9.dll','d3d10core.dll','openvr_api_dxvk.dll'):
    shutil.copy2(base/'wine/dxvk'/name, system/name)
for name in ('libvkd3d-1.dll','libvkd3d-shader-1.dll'):
    shutil.copy2(base/'vkd3d'/name, system/name)
(prefix/'vrclient/bin').mkdir(parents=True, exist_ok=True)
shutil.copy2(base/'wine/x86_64-windows/vrclient_x64.dll', prefix/'vrclient/bin/vrclient_x64.dll')
windows = prefix/'users/steamuser/AppData/Local/openvr/openvrpaths.vrpath'
linux = Path('/home/ga/.config/openvr/openvrpaths.vrpath')
for path, runtime in ((windows, r'C:\vrclient'), (linux, '/opt/engine-gpu/vr/xrizer-v0.5')):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'version': 1, 'jsonid': 'vrpathreg', 'runtime': [runtime],
                               'config': [], 'log': [], 'external_drivers': []})+'\n')
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name')
    parser.add_argument('command', choices=('prepare', 'run'))
    parser.add_argument('--manifest', type=Path, default=Path('runs/racing/windows-files.json'))
    parser.add_argument('--timeout', type=int, default=45)
    parser.add_argument('--novr', action='store_true')
    parser.add_argument('--experimental-vulkan13', action='store_true',
                        help='temporarily permit Primus-VK for Vulkan 1.3; not conformance validation')
    args = parser.parse_args()
    manager = EnvironmentManager()
    state = manager.status(args.name)
    if not args.name.startswith('vr-racing-') or state['status'] != 'running' or not state.get('gpu'):
        parser.error('select a running disposable GPU sandbox named vr-racing-*')
    if not 5 <= args.timeout <= 300:
        parser.error('timeout must be 5..300 seconds')
    if args.command == 'prepare' and (args.novr or args.experimental_vulkan13):
        parser.error('rendering options apply to run')
    prefix = [*manager._command(args.name), 'exec', args.name]

    def guest(*command, payload=None, check=True):
        result = subprocess.run([*prefix, *command], input=payload, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        if check and result.returncode:
            raise RuntimeError(result.stdout)
        return result

    user = ['runuser','-u','ga','--','env','HOME=/home/ga','DISPLAY=:1',
            'XAUTHORITY=/home/ga/.Xauthority','XDG_RUNTIME_DIR=/run/user/1000',
            'WINEPREFIX=/home/ga/.local/share/ams2-wine','WINEDEBUG=-all,err+all',
            'WINEDLLOVERRIDES=mscoree,mshtml=;dxgi,d3d11,d3d9,d3d10core=n',
            'XR_RUNTIME_JSON=/opt/vr/monado/share/openxr/1/openxr_monado.json',
            'PROTON_VR_RUNTIME=/opt/engine-gpu/vr/xrizer-v0.5']
    wine = '/opt/engine-gpu/vr/GE-Proton9-27/files/bin/wine64'
    if args.command == 'prepare':
        guest('python3', '-c', PREPARE, payload=args.manifest.read_text())
        guest('sh', '-c', 'cat > /opt/vr/vr-vulkan-xvnc.sh',
              payload=(manager.lab/'scripts/vr-vulkan-xvnc.sh').read_text())
        if guest('test', '-f', '/home/ga/.local/share/ams2-wine/system.reg', check=False).returncode:
            print(guest(*user, 'timeout', '-k', '3', '90', wine, 'wineboot', '-u').stdout, end='')
        guest(*user, 'python3', '-c', VR_FILES)
        print('Prepared asset links and the Wine/DXVK/OpenVR bridge.')
        return
    guest(*user, 'test', '-r', '/opt/ams2/AMS2Demo.exe')
    if not args.novr:
        guest('systemctl', 'is-active', 'vr-monado-live')
    manifest = '/usr/share/vulkan/implicit_layer.d/primus_vk.json'
    original = None
    try:
        if args.experimental_vulkan13:
            original = guest('cat', manifest).stdout
            modified = json.loads(original)
            modified['layer']['api_version'] = '1.3.0'
            guest('sh', '-c', 'cat > "$1"', 'sh', manifest, payload=json.dumps(modified)+'\n')
        # The ordinary executable launches AMS2DemoAVX.exe and exits. Keep its
        # dedicated Wine server and all children inside one bounded guest unit.
        server = '/opt/engine-gpu/vr/GE-Proton9-27/files/bin/wineserver'
        stopped = guest(*user, server, '-k', check=False)
        if stopped.returncode not in (0, 1) or stopped.stdout.strip():
            raise RuntimeError('Could not stop the dedicated Wine server: '+stopped.stdout)
        command = 'cd /opt/ams2 && '+wine+' AMS2Demo.exe'
        if args.novr:
            command += ' -novr'
        command += '; result=$?; '+server+' -w; exit "$result"'
        result = guest('systemd-run', '--unit=ams2-probe', '--collect', '--wait', '--pipe',
                       '--property=RuntimeMaxSec='+str(args.timeout),
                       '--property=TimeoutStopSec=3', *user,
                       'sh', '/opt/vr/vr-vulkan-xvnc.sh', 'sh', '-c', command, check=False)
        print(result.stdout, end='')
        crash = any(marker in result.stdout for marker in
                    ('wine: Unhandled', 'Unhandled exception:', 'Unhandled page fault'))
        print(json.dumps({'service_exit_code': result.returncode,
                          'crash_reported': crash, 'novr_argument': args.novr,
                          'gameplay_verified': False,
                          'experimental_vulkan13': args.experimental_vulkan13}))
    finally:
        if original is not None:
            guest('sh', '-c', 'cat > "$1"', 'sh', manifest, payload=original)
    raise SystemExit(result.returncode or (1 if crash else 0))


if __name__ == '__main__':
    main()

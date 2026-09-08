#!/usr/bin/env python3
"""Reproduce Alyx startup probes in a disposable, prepared VR sandbox.

Requires the user's staged installation, GE-Proton9-27, xrizer-v0.5 and
prepare-vr-lab.py. These are startup diagnostics, not gameplay acceptance.
"""
import argparse
import json
from pathlib import Path
import subprocess

from environment import EnvironmentManager

PREPARE = r'''
import json, os, shutil, sys
from pathlib import Path, PurePosixPath
source, root = Path('/opt/engine-gpu/alyx'), Path('/opt/alyx')
entries = json.load(sys.stdin)
for entry in entries:
    path = PurePosixPath(entry['path'])
    if path.is_absolute() or '..' in path.parts or ':' in str(path) or chr(92) in str(path):
        raise ValueError('invalid installation path')
for entry in entries:
    relative = entry['path']
    target, original = root / relative, source / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and not target.is_symlink():
        continue
    if original.is_file() and (target.suffix in ('.cfg', '.vcfg', '.json') or 'cfg' in target.parts):
        if target.is_symlink():
            if target.readlink() != original:
                raise ValueError('unexpected existing symlink')
            target.unlink()
        shutil.copy2(original, target)
        os.chown(target, 1000, 1000)
    elif not target.is_symlink():
        target.symlink_to(original)
for directory, children, files in os.walk(root):
    os.chown(directory, 1000, 1000)
    os.chmod(directory, 0o755)
'''

VR_FILES = r'''
import json, shutil
from pathlib import Path
base = Path('/opt/engine-gpu/vr/GE-Proton9-27/files/lib64')
prefix = Path('/home/ga/.local/share/alyx-wine/drive_c')
system = prefix / 'windows/system32'
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
    parser.add_argument('command', choices=('prepare', 'monado', 'native', 'windows', 'cube'))
    parser.add_argument('--manifest', type=Path, default=Path('runs/alyx/windows-files.json'))
    parser.add_argument('--timeout', type=int, default=45)
    parser.add_argument('--experimental-vulkan13', action='store_true',
                        help='temporarily permit Primus-VK for Vulkan 1.3; not conformance validation')
    args = parser.parse_args()
    manager = EnvironmentManager()
    state = manager.status(args.name)
    if not args.name.startswith('vr-') or state['status'] != 'running' or not state.get('gpu'):
        parser.error('select a running disposable GPU sandbox named vr-*')
    if not 5 <= args.timeout <= 300:
        parser.error('timeout must be 5..300 seconds')
    if args.experimental_vulkan13 and args.command not in ('windows', 'cube'):
        parser.error('the API-version experiment applies to windows or cube')
    prefix = [*manager._command(args.name), 'exec', args.name]

    def guest(*command, payload=None, check=True):
        result = subprocess.run([*prefix, *command], input=payload, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        if check and result.returncode:
            raise RuntimeError(result.stdout)
        return result

    user = ['runuser', '-u', 'ga', '--', 'env', 'HOME=/home/ga', 'DISPLAY=:1',
            'XAUTHORITY=/home/ga/.Xauthority', 'XDG_RUNTIME_DIR=/run/user/1000',
            'WINEPREFIX=/home/ga/.local/share/alyx-wine', 'WINEDEBUG=-all,err+all',
            'WINEDLLOVERRIDES=mscoree,mshtml=;dxgi,d3d11,d3d9,d3d10core=n',
            'XR_RUNTIME_JSON=/opt/vr/monado/share/openxr/1/openxr_monado.json',
            'PROTON_VR_RUNTIME=/opt/engine-gpu/vr/xrizer-v0.5']
    wine = '/opt/engine-gpu/vr/GE-Proton9-27/files/bin/wine64'
    if args.command == 'prepare':
        print(guest('python3', '-c', PREPARE, payload=args.manifest.read_text()).stdout, end='')
        print(guest('sh', '-c', 'cat > /opt/vr/vr-vulkan-xvnc.sh',
                    payload=(manager.lab/'scripts/vr-vulkan-xvnc.sh').read_text()).stdout, end='')
        if guest('test', '-f', '/home/ga/.local/share/alyx-wine/system.reg', check=False).returncode:
            print(guest(*user, 'timeout', '-k', '3', '90', wine, 'wineboot', '-u').stdout, end='')
        print(guest(*user, 'python3', '-c', VR_FILES).stdout, end='')
        print('Prepared writable directories/configs, read-only asset links, and the Wine VR bridge.')
        return
    if args.command == 'monado':
        if guest('pgrep', '-x', 'monado-service', check=False).returncode == 0:
            parser.error('Monado is already running in this sandbox')
        print(guest('systemd-run', '--unit=vr-monado-live', '--uid=ga', '--collect',
                    '--setenv=VR_HZ=90', '/usr/local/bin/engine-gpu',
                    'sh', '/opt/vr/vr-monado-guest.sh', 'monado').stdout, end='')
        return
    if args.command == 'windows':
        guest('systemctl', 'is-active', 'vr-monado-live')
        command = ['sh', '/opt/vr/vr-vulkan-xvnc.sh', 'sh', '-c',
                   'cd /opt/alyx/game && exec '+wine+' bin/win64/hlvr.exe '
                   '-vr -noasserts -nopassiveasserts -console -condebug']
    elif args.command == 'native':
        command = ['/usr/local/bin/engine-gpu', 'env', 'STEAM_RUNTIME=0',
                   'VK_DRIVER_FILES=/opt/vr/vulkan.json', 'VK_ICD_FILENAMES=/opt/vr/vulkan.json',
                   'bash', '/opt/alyx/game/hlvr.sh', '-vr', '-steam', '-vulkan',
                   '-noasserts', '-nopassiveasserts']
    else:
        command = ['sh', '/opt/vr/vr-vulkan-xvnc.sh', 'vkcube', '--c', '60']
    manifest = '/usr/share/vulkan/implicit_layer.d/primus_vk.json'
    original = None
    try:
        if args.experimental_vulkan13:
            original = guest('cat', manifest).stdout
            modified = json.loads(original)
            modified['layer']['api_version'] = '1.3.0'
            guest('sh', '-c', 'cat > "$1"', 'sh', manifest, payload=json.dumps(modified)+'\n')
        result = guest(*user, 'timeout', '-k', '3', str(args.timeout), *command, check=False)
        print(result.stdout, end='')
        print(json.dumps({'probe': args.command, 'exit_code': result.returncode,
                          'experimental_vulkan13': args.experimental_vulkan13}))
    finally:
        if original is not None:
            guest('sh', '-c', 'cat > "$1"', 'sh', manifest, payload=original)
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()

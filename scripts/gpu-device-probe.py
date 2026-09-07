#!/usr/bin/env python3
"""Verify the live single-device boundary, including guest-root mknod."""
import glob
import json
import os
import stat

assert not os.path.exists('/dev/kvm')
devices = sorted(glob.glob('/dev/nvidia*'))
gpu_paths = [path for path in devices if path.removeprefix('/dev/nvidia').isdigit()]
assert len(gpu_paths) == 1, devices
assert devices == sorted([gpu_paths[0], '/dev/nvidiactl', '/dev/nvidia-uvm']), devices
fd = os.open(gpu_paths[0], os.O_RDWR)
os.close(fd)
selected = int(gpu_paths[0].removeprefix('/dev/nvidia'))
other = 1 if selected == 0 else 0
path = f'/dev/nvidia{other}'
info = os.stat(gpu_paths[0])
created = False
try:
    try:
        os.mknod(path, stat.S_IFCHR | 0o666, os.makedev(os.major(info.st_rdev), other))
        created = True
        fd = os.open(path, os.O_RDWR)
    except OSError as error:
        denied = {'errno': error.errno, 'message': str(error), 'mknod_succeeded': created}
    else:
        os.close(fd)
        raise AssertionError('guest root opened an unselected GPU')
finally:
    if created:
        os.unlink(path)
print(json.dumps({'passed': True, 'devices': devices, 'kvm_present': False,
                  'unselected_device_open': denied}), flush=True)

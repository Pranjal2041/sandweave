"""Single allocated NVIDIA GPU configuration for the standalone lab."""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess


def allocated_device(index):
    if not re.fullmatch(r'0|[1-9][0-9]*', str(index)):
        raise ValueError('GPU must be a physical numeric index')
    allocation = os.environ.get('SLURM_STEP_GPUS') or os.environ.get('SLURM_JOB_GPUS', '')
    if not allocation or any(not part.isdecimal() for part in allocation.split(',')):
        raise ValueError('requires an explicit numeric Slurm GPU allocation')
    if str(index) not in allocation.split(','):
        raise ValueError(f'GPU {index} is outside the Slurm allocation {allocation}')
    paths = [f'/dev/nvidia{index}', '/dev/nvidiactl', '/dev/nvidia-uvm']
    devices = []
    for path in paths:
        info = Path(path).stat()
        if not stat.S_ISCHR(info.st_mode):
            raise ValueError(f'not a character device: {path}')
        devices.append({'path': path, 'type': 'c', 'major': os.major(info.st_rdev),
                        'minor': os.minor(info.st_rdev), 'fileMode': 0o666, 'uid': 0, 'gid': 0})
    return devices


def driver_version():
    match = re.search(r'Kernel Module[^\n]*?\s(\d+\.\d+\.\d+)\s', Path('/proc/driver/nvidia/version').read_text())
    if not match:
        raise ValueError('cannot identify the loaded NVIDIA driver')
    return match.group(1)


def device_identity(index):
    for info_path in Path('/proc/driver/nvidia/gpus').glob('*/information'):
        fields = dict(line.split(':', 1) for line in info_path.read_text().splitlines() if ':' in line)
        fields = {key.strip(): value.strip() for key, value in fields.items()}
        if fields.get('Device Minor') != str(index):
            continue
        visible = subprocess.check_output([
            'nvidia-smi', '--query-gpu=index,uuid', '--format=csv,noheader'], text=True)
        for line in visible.splitlines():
            nvml_index, uuid = (part.strip() for part in line.split(','))
            if uuid == fields['GPU UUID']:
                return {'device_minor': index, 'uuid': uuid,
                        'host_nvml_index': int(nvml_index), 'pci_bus_id': fields['Bus Location']}
        raise ValueError('selected device is not visible to host NVML in this allocation')
    raise ValueError(f'cannot identify /dev/nvidia{index}')


def stage_driver(destination):
    """Copy host user-space driver libraries without touching host installation."""
    version = driver_version()
    destination.mkdir(parents=True, exist_ok=False)
    lib = destination / 'lib'
    lib.mkdir()
    copied = {}
    cache = subprocess.check_output(['ldconfig', '-p'], text=True)
    for line in cache.splitlines():
        match = re.search(r'^\s*(\S+) \(libc6,x86-64[^)]*\) => (\S+)', line)
        if not match:
            continue
        name, path = match.groups()
        if not name.startswith(('libnvidia-', 'libcuda.', 'libcudadebugger.', 'libEGL_nvidia.', 'libGLX_nvidia.')):
            continue
        source = Path(path).resolve()
        if source.name not in copied:
            shutil.copy2(source, lib / source.name)
            copied[source.name] = str(source)
        if name != source.name and not (lib / name).exists():
            (lib / name).symlink_to(source.name)
    for name in ('libcuda.so.1', 'libnvidia-ml.so.1', 'libEGL_nvidia.so.0', 'libGLX_nvidia.so.0'):
        if not (lib / name).is_file():
            raise ValueError(f'missing driver library {name}')
    (destination / 'bin').mkdir()
    shutil.copy2(shutil.which('nvidia-smi'), destination / 'bin/nvidia-smi')
    for source, name in [('/usr/share/glvnd/egl_vendor.d/10_nvidia.json', 'egl.json'),
                         ('/usr/share/vulkan/icd.d/nvidia_icd.x86_64.json', 'vulkan.json')]:
        shutil.copy2(source, destination / name)
    metadata = {'driver_version': version, 'sources': copied}
    (destination / 'driver.json').write_text(json.dumps(metadata, indent=2) + '\n')
    return metadata


def configure(spec, index, resources, lab):
    devices = allocated_device(index)
    identity = device_identity(index)
    driver = json.loads((resources / 'driver/driver.json').read_text())
    if driver['driver_version'] != driver_version():
        raise ValueError('staged user-space GPU driver does not match the loaded host driver')
    if not resources.resolve().is_relative_to(lab.resolve()):
        raise ValueError('GPU resources must be staged inside the lab')
    spec['linux']['devices'] = devices
    spec['mounts'].append({'destination': '/opt/engine-gpu', 'type': 'bind',
                           'source': '/lab/' + str(resources.resolve().relative_to(lab.resolve())),
                           'options': ['bind', 'ro']})
    spec['process']['env'] += [f'NVIDIA_VISIBLE_DEVICES={identity["uuid"]}', 'CUDA_VISIBLE_DEVICES=0',
                               'NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics,video']
    spec['process']['args'] = ['/usr/local/bin/engine-gpu-init', *spec['process']['args']]
    spec['annotations']['dev.gvisor.internal.nvproxy'] = 'true'
    return {**identity, 'driver_version': driver['driver_version'],
            'resources': str(resources), 'devices': devices,
            'snapshot_support': 'persistent filesystem cold restore; experimental CUDA live restore; graphics live restore unsupported'}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-device', type=int)
    parser.add_argument('--stage-driver', type=Path)
    args = parser.parse_args()
    if args.check_device is not None:
        for device in allocated_device(args.check_device):
            print(device['path'])
    elif args.stage_driver:
        print(json.dumps(stage_driver(args.stage_driver), indent=2))
    else:
        parser.error('specify --check-device or --stage-driver')

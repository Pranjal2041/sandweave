"""Single allocated NVIDIA GPU configuration for the standalone lab."""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import xml.etree.ElementTree as ET


def gpu_inventory():
    """Read device minors and UUIDs through NVML, including hosts without procfs driver entries."""
    report = ET.fromstring(subprocess.check_output(['nvidia-smi', '-q', '-x'], text=True))
    version = report.findtext('kmd_version') or report.findtext('driver_version')
    if not version or not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('cannot identify the loaded NVIDIA driver')
    devices = []
    for ordinal, gpu in enumerate(report.findall('gpu')):
        minor, identifier = gpu.findtext('minor_number'), gpu.findtext('uuid')
        if not minor or not minor.isdecimal() or not identifier or not identifier.startswith('GPU-'):
            raise ValueError('NVIDIA inventory lacks a device minor or UUID')
        devices.append({'device_minor': int(minor), 'uuid': identifier,
                        'host_nvml_index': ordinal, 'pci_bus_id': gpu.attrib['id']})
    return version, devices


def eligible_devices():
    limit = os.environ.get('SANDWEAVE_GPU_LIMIT')
    if limit == '':
        return []
    devices = _eligible_devices()
    if limit is not None:
        if any(not part.isdecimal() for part in limit.split(',')):
            raise ValueError('SANDWEAVE_GPU_LIMIT must list numeric device minors')
        allowed = set(map(int, limit.split(',')))
        devices = [device for device in devices if device in allowed]
    return devices


def _eligible_devices():
    allocation = os.environ.get('SLURM_STEP_GPUS') or os.environ.get('SLURM_JOB_GPUS')
    if allocation:
        if any(not part.isdecimal() for part in allocation.split(',')):
            raise ValueError('this runtime requires a Slurm allocation with numeric full-GPU device minors')
        return list(map(int, allocation.split(',')))
    if os.environ.get('SLURM_JOB_ID'):
        return []  # A CPU-only allocation must never discover unallocated GPUs.
    explicit = os.environ.get('SANDWEAVE_GPU_DEVICES')
    if explicit is not None:
        if explicit == '':
            return []
        if any(not part.isdecimal() for part in explicit.split(',')):
            raise ValueError('SANDWEAVE_GPU_DEVICES must list numeric device minors')
        return list(map(int, explicit.split(',')))
    # Local/SSH workers respect the container's visibility filters and actual
    # device permissions. Numeric CUDA visibility uses NVML ordinals, not minors.
    _, visible = gpu_inventory()
    selected = []
    for device in visible:
        ordinal, identifier = str(device['host_nvml_index']), device['uuid']
        eligible = True
        for key in ('CUDA_VISIBLE_DEVICES', 'NVIDIA_VISIBLE_DEVICES'):
            value = os.environ.get(key)
            if value is None or value == 'all':
                continue
            if not any(item == ordinal or (item.startswith('GPU-') and identifier.startswith(item))
                       for item in value.split(',')):
                eligible = False
        if not eligible:
            continue
        minor = device['device_minor']
        try:
            fd = os.open('/dev/nvidia' + str(minor), os.O_RDWR)
        except OSError:
            continue
        os.close(fd)
        selected.append(minor)
    return selected


def allocated_device(index):
    if not re.fullmatch(r'0|[1-9][0-9]*', str(index)):
        raise ValueError('GPU must be a physical numeric index')
    if int(index) not in eligible_devices():
        raise ValueError(f'GPU {index} is outside this worker\'s eligible devices')
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
    return gpu_inventory()[0]


def device_identity(index):
    for device in gpu_inventory()[1]:
        if device['device_minor'] == index:
            return device
    raise ValueError(f'cannot identify /dev/nvidia{index}')


def stage_driver(destination):
    """Copy host user-space driver libraries without touching host installation."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    temporary = Path(tempfile.mkdtemp(prefix='.driver-', dir=destination.parent))
    try:
        metadata = _copy_driver(temporary)
        temporary.rename(destination)
        return metadata
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _copy_driver(destination):
    version = driver_version()
    lib = destination / 'lib'
    lib.mkdir()
    copied = {}
    ldconfig = shutil.which('ldconfig') or next(
        (str(path) for path in (Path('/sbin/ldconfig'), Path('/usr/sbin/ldconfig')) if path.is_file()), None)
    if ldconfig is None:
        raise ValueError('Cannot locate ldconfig to read the installed NVIDIA libraries')
    cache = subprocess.check_output([ldconfig, '-p'], text=True)
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
    for name in ('libcuda.so.1', 'libnvidia-ml.so.1'):
        if not (lib / name).is_file():
            raise ValueError(f'missing driver library {name}')
    (destination / 'bin').mkdir()
    smi = shutil.which('nvidia-smi')
    if smi is None:
        raise ValueError('nvidia-smi is missing from this worker')
    shutil.copy2(smi, destination / 'bin/nvidia-smi')
    # Host distributions use different JSON filenames and library paths. The
    # guest descriptors refer only to libraries staged at the guest mount.
    (destination / 'egl.json').write_text(json.dumps({'file_format_version': '1.0.0',
        'ICD': {'library_path': '/opt/engine-gpu/driver/lib/libEGL_nvidia.so.0'}}) + '\n')
    (destination / 'vulkan.json').write_text(json.dumps({'file_format_version': '1.0.0',
        'ICD': {'library_path': '/opt/engine-gpu/driver/lib/libGLX_nvidia.so.0', 'api_version': '1.3.0'}}) + '\n')
    metadata = {'driver_version': version, 'sources': copied,
                'graphics': all((lib / name).is_file() for name in ('libEGL_nvidia.so.0', 'libGLX_nvidia.so.0'))}
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
    # The staged driver resources are immutable for this sandbox's lifetime.
    spec['annotations'].update({
        'dev.gvisor.spec.mount.gpu.source': '/lab/' + str(resources.resolve().relative_to(lab.resolve())),
        'dev.gvisor.spec.mount.gpu.type': 'bind',
        'dev.gvisor.spec.mount.gpu.share': 'container',
        'dev.gvisor.spec.mount.gpu.options': 'ro',
    })
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

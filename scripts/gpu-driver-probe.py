#!/usr/bin/env python3
"""Check ordinary guest-user driver loading without engine-gpu environment paths."""
import ctypes
import json
import os
from pathlib import Path
import subprocess

assert not os.environ.get('LD_LIBRARY_PATH')
assert not os.environ.get('LD_PRELOAD')
assert os.getuid() == 1000
nvml = ctypes.CDLL('libnvidia-ml.so.1')
assert nvml.nvmlInit_v2() == 0
version = ctypes.create_string_buffer(80)
assert nvml.nvmlSystemGetDriverVersion(version, len(version)) == 0
assert nvml.nvmlShutdown() == 0
cuda = ctypes.CDLL('libcuda.so.1')
assert cuda.cuInit(0) == 0
count = ctypes.c_int()
assert cuda.cuDeviceGetCount(ctypes.byref(count)) == 0
assert count.value == 1
paths = sorted({line.split()[-1] for line in Path('/proc/self/maps').read_text().splitlines()
                if 'libnvidia-ml.so' in line or 'libcuda.so' in line})
assert len(paths) == 2 and all(path.startswith('/opt/engine-gpu/driver/lib/') for path in paths), paths
expected = json.loads(Path('/opt/engine-gpu/driver/driver.json').read_text())['driver_version']
assert version.value.decode() == expected
smi = subprocess.check_output(['nvidia-smi', '--query-gpu=name,driver_version,uuid',
                               '--format=csv,noheader'], text=True).strip()
assert expected in smi
print(json.dumps({'passed': True, 'uid': os.getuid(), 'driver_version': expected,
                  'cuda_device_count': count.value, 'loaded_libraries': paths,
                  'nvidia_smi': smi, 'library_environment_override': False}))

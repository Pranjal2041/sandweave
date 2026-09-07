#!/usr/bin/env python3
"""Install the pinned NVIDIA utility for optional CUDA live-checkpoint probes."""
import hashlib
import json
from pathlib import Path
import urllib.request

revision = '00d5cce84c628088d6caa203fc4af40c1538b6f7'
sha256 = '707fa7f54136824d6c1d6dd724b9b1717610f831033c00d06da474de363a06db'
url = f'https://raw.githubusercontent.com/NVIDIA/cuda-checkpoint/{revision}/bin/x86_64_Linux/cuda-checkpoint'
lab = Path(__file__).resolve().parent.parent
with urllib.request.urlopen(url, timeout=60) as response:
    data = response.read()
if hashlib.sha256(data).hexdigest() != sha256:
    raise ValueError('downloaded CUDA checkpoint utility does not match its pinned digest')
target = lab / 'tools/gpu/bin/cuda-checkpoint'
target.parent.mkdir(parents=True, exist_ok=True)
temporary = target.with_suffix('.new')
temporary.write_bytes(data)
temporary.chmod(0o755)
temporary.replace(target)
print(json.dumps({'revision': revision, 'url': url, 'sha256': sha256, 'path': str(target)}))

#!/usr/bin/env python3
"""Export clean Docker state from a lab guest for a subsequent cold boot."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

lab = Path(__file__).resolve().parent.parent
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('name')
p.add_argument('output', type=Path)
a = p.parse_args()
if not re.fullmatch(r'[a-zA-Z0-9_-]+', a.name):
    p.error('invalid sandbox name')
output = a.output.resolve()
output.parent.mkdir(parents=True, exist_ok=True)
if output.exists():
    p.error('refusing to overwrite an existing archive')
base = [str(lab / 'scripts/gvisor-host.sh'), '/lab/tools/gvisor-socket/runsc',
        '--root=/local/gvisor/state', 'exec', a.name]
start = time.perf_counter()
stop = ['/bin/bash', '-c', 'set -e; docker stop -t 30 general-vm-moodle; systemctl stop docker.socket docker.service containerd.service']
result = subprocess.run(base + stop, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
print(result.stdout.decode(errors='replace'), end='', flush=True)
result.check_returncode()
try:
    temporary = output.with_suffix(output.suffix + '.partial')
    with temporary.open('xb') as target:
        proc = subprocess.Popen(base + ['tar', '--numeric-owner', '--xattrs', '--acls', '--sparse',
                                       '-cpf', '-', '-C', '/var/lib', 'docker', 'containerd'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        digest = hashlib.sha256()
        for block in iter(lambda: proc.stdout.read(4*1024*1024), b''):
            digest.update(block)
            target.write(block)
        errors = proc.stderr.read()
        code = proc.wait()
        if code:
            raise RuntimeError(f'tar failed ({code}): {errors.decode(errors="replace")}')
    temporary.replace(output)
    manifest = {'archive': str(output), 'bytes': output.stat().st_size,
                'sha256': digest.hexdigest(), 'seconds': time.perf_counter()-start}
    output.with_suffix('.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print(json.dumps(manifest, indent=2), flush=True)
finally:
    result = subprocess.run(base + ['/bin/bash', '-c', 'set -e; systemctl start containerd docker.socket docker; docker start general-vm-moodle'],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(result.stdout.decode(errors='replace'), end='', flush=True)
    result.check_returncode()

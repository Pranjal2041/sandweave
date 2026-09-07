#!/bin/bash
set -euo pipefail
lab=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$lab/tools/fast-io"
gcc -std=c11 -O3 -Wall -Wextra -Werror "$lab/sources/xvnc-fast-io.c" \
  -o "$lab/tools/fast-io/bridge.tmp" -lxcb -lxcb-shm -lxcb-xtest
mv "$lab/tools/fast-io/bridge.tmp" "$lab/tools/fast-io/bridge"
# The base desktop has XCB and SHM, but lacks the small XTEST client library.
cp -L /usr/lib64/libxcb-xtest.so.0 "$lab/tools/fast-io/libxcb-xtest.so.0"
python - "$lab" <<'PY'
import hashlib, json, subprocess, sys
from pathlib import Path
lab = Path(sys.argv[1])
files = ['sources/xvnc-fast-io.c', 'tools/fast-io/bridge', 'tools/fast-io/libxcb-xtest.so.0']
record = {'sha256': {file: hashlib.sha256((lab/file).read_bytes()).hexdigest() for file in files},
          'compiler': subprocess.check_output(['gcc', '--version'], text=True).splitlines()[0],
          'system_packages': subprocess.check_output(['rpm', '-q', 'libxcb', 'libxcb-devel', 'glibc'], text=True).splitlines()}
(lab/'tools/fast-io/build.json').write_text(json.dumps(record, indent=2)+'\n')
PY

#!/bin/bash
set -euo pipefail
mkdir -p /var/tmp/engine-persist
printf 'checkpoint retains guest state\n' > /var/tmp/engine-persist/secret
chown 1234:5678 /var/tmp/engine-persist/secret
chmod 640 /var/tmp/engine-persist/secret
python3 - <<'PY'
import os
os.setxattr('/var/tmp/engine-persist/secret','user.engine',b'original-value')
PY
echo PERSIST_READY
exec sleep infinity

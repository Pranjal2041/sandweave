#!/bin/bash
set -euo pipefail
lab_root=$(cd "$(dirname "$0")/.." && pwd -P)
exec ssh -i "$lab_root/tools/id_ed25519" -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$lab_root/tools/known_hosts" \
  -o ConnectTimeout=5 -p "${LAB_SSH_PORT:-22022}" root@127.0.0.1 "$@"

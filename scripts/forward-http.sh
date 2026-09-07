#!/bin/bash
set -euo pipefail
lab_root=$(cd "$(dirname "$0")/.." && pwd -P)
exec ssh -i "$lab_root/tools/id_ed25519" -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$lab_root/tools/known_hosts" \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -N \
  -L "127.0.0.1:${LAB_HTTP_PORT:-28082}:127.0.0.1:80" \
  -p "${LAB_SSH_PORT:-22022}" root@127.0.0.1

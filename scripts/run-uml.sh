#!/bin/bash
set -euo pipefail
lab_root=$(cd "$(dirname "$0")/.." && pwd -P)
lab_local=$(cat "$lab_root/runs/local-path.txt")
export APPTAINER_CACHEDIR="$lab_root/downloads/apptainer-cache"
export APPTAINER_TMPDIR="$lab_root/runs/apptainer-tmp"
exec apptainer exec --userns --containall --cleanenv --no-home \
  --env "LAB_SSH_PORT=${LAB_SSH_PORT:-22022}" --env "LAB_VNC_PORT=${LAB_VNC_PORT:-25901}" \
  --bind "$lab_root:/lab" --bind "$lab_local:$lab_local" \
  "$lab_root/tools/debian-trixie.sif" \
  sh -c 'set -eu; test ! -e /dev/kvm; echo "HOST_BOUNDARY: /dev/kvm absent"; id; export TMPDIR=/dev/shm; export PATH=/lab/scripts:/lab/tools/network/usr/bin:$PATH; export LD_LIBRARY_PATH=/lab/tools/network/usr/lib/x86_64-linux-gnu; export VDEPLUGIN_PATH=/lab/tools/network/usr/lib/x86_64-linux-gnu/vdeplug; exec "$@"' sh "$@"

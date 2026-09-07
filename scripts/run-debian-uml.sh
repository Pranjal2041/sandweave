#!/bin/bash
set -euo pipefail
lab_root=$(cd "$(dirname "$0")/.." && pwd -P)
export APPTAINER_CACHEDIR="$lab_root/downloads/apptainer-cache"
export APPTAINER_TMPDIR="$lab_root/runs/apptainer-tmp"
exec apptainer exec --userns --containall --cleanenv --no-home \
  --bind "$lab_root:/lab" "$lab_root/tools/debian-trixie.sif" \
  sh -c 'set -eu; test ! -e /dev/kvm; echo "HOST_BOUNDARY: /dev/kvm absent"; id; export TMPDIR=/dev/shm; exec /lab/tools/uml/usr/bin/linux.uml "$@"' sh "$@"

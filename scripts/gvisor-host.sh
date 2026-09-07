#!/bin/bash
set -euo pipefail
lab_root=$(cd "$(dirname "$0")/.." && pwd -P)
lab_local=$(cat "$lab_root/runs/local-path.txt")
gpu_binds=()
if [[ "${1:-}" == --gpu ]]; then
  gpu_paths=$(python "$lab_root/scripts/gvisor_gpu.py" --check-device "$2")
  while IFS= read -r gpu_path; do
    gpu_binds+=(--bind "$gpu_path:$gpu_path")
  done <<< "$gpu_paths"
  shift 2
fi
export APPTAINER_CACHEDIR="$lab_root/downloads/apptainer-cache"
export APPTAINER_TMPDIR="$lab_root/runs/apptainer-tmp"
mkdir -p "$lab_local/gvisor-tmp" "$APPTAINER_TMPDIR" "$APPTAINER_CACHEDIR"
exec apptainer exec --userns --contain --ipc --cleanenv --no-home \
  "${gpu_binds[@]}" \
  --bind "$lab_root:/lab" --bind "$lab_local:/local" \
  --bind "$lab_local/gvisor-tmp:/tmp" --pwd /lab \
  "$lab_root/tools/debian-trixie.sif" \
  sh -c 'set -eu; test ! -e /dev/kvm; export PATH=/lab/tools/gvisor-nightly-20260906:/lab/tools/erofs/usr/bin:$PATH; exec "$@"' sh "$@"

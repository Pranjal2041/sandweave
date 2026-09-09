#!/bin/bash
set -euo pipefail
lab_root=$(cd "$(dirname "$0")/.." && pwd -P)
lab_local=$(cat "$lab_root/runs/local-path.txt")
gpu_binds=()
while [[ "${1:-}" == --gpu || "${1:-}" == --mounts ]]; do
  if [[ "$1" == --mounts ]]; then
    # A failed parser must abort, not launch with a partial mount list.
    mount_args=$(mktemp "$lab_root/runs/.mount-args.XXXXXX")
    trap 'rm -f "$mount_args"' EXIT
    "${SANDWEAVE_PYTHON:-python3}" "$lab_root/scripts/external_mounts.py" "$2" > "$mount_args"
    while IFS= read -r -d '' mount_arg; do
      gpu_binds+=(--bind "$mount_arg")
    done < "$mount_args"
    rm -f "$mount_args"
    trap - EXIT
    shift 2
    continue
  fi
  gpu_paths=$("${SANDWEAVE_PYTHON:-python3}" "$lab_root/scripts/gvisor_gpu.py" --check-device "$2")
  while IFS= read -r gpu_path; do
    gpu_binds+=(--bind "$gpu_path:$gpu_path")
  done <<< "$gpu_paths"
  shift 2
done
export APPTAINER_CACHEDIR="$lab_root/downloads/apptainer-cache"
export APPTAINER_TMPDIR="$lab_root/runs/apptainer-tmp"
mkdir -p "$lab_local/gvisor-tmp" "$APPTAINER_TMPDIR" "$APPTAINER_CACHEDIR"
exec apptainer exec --userns --contain --ipc --cleanenv --no-home \
  "${gpu_binds[@]}" \
  --bind "$lab_root:/lab" --bind "$lab_local:/local" \
  --bind "$lab_local/gvisor-tmp:/tmp" --pwd /lab \
  "$lab_root/tools/debian-trixie.sif" \
  sh -c 'set -eu; test ! -e /dev/kvm; export PATH=/lab/tools/gvisor-nightly-20260906:/lab/tools/erofs/usr/bin:$PATH; exec "$@"' sh "$@"

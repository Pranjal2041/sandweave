#!/bin/bash
set -euo pipefail
lab_root=$(cd "$(dirname "$0")/.." && pwd -P)
lab_local=$(cat "$lab_root/runs/local-path.txt")
gpu_binds=()
kvm=0
while [[ "${1:-}" == --gpu || "${1:-}" == --mounts || "${1:-}" == --disk-memory || "${1:-}" == --storage || "${1:-}" == --kvm ]]; do
  if [[ "$1" == --kvm ]]; then
    # Only an explicit KVM launch sees the host device; every other launch asserts it is absent.
    kvm=1
    gpu_binds+=(--bind /dev/kvm:/dev/kvm)
    shift
    continue
  fi
  if [[ "$1" == --storage ]]; then
    gpu_binds+=(--bind "$2:/sandbox-storage")
    shift 2
    continue
  fi
  if [[ "$1" == --disk-memory ]]; then
    gpu_binds+=(--bind "$2:/disk-memory")
    shift 2
    continue
  fi
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
exec "${SANDWEAVE_PYTHON:-python3}" "$lab_root/scripts/host_compat.py" "$lab_root/tools/debian-trixie.sif" \
  --contain --ipc --cleanenv --no-home \
  "${gpu_binds[@]}" \
  --bind "$lab_root:/lab" --bind "$lab_local:/local" \
  --bind "$lab_local/gvisor-tmp:/tmp" --pwd /lab \
  -- \
  sh -c 'set -eu; if [ "$1" = 1 ]; then test -c /dev/kvm; else test ! -e /dev/kvm; fi; shift; export PATH=/lab/tools/gvisor-nightly-20260906:/lab/tools/erofs/usr/bin:$PATH; exec "$@"' sh "$kvm" "$@"

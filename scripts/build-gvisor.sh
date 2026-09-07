#!/bin/bash
set -euo pipefail
lab_root=$(cd "$(dirname "$0")/.." && pwd -P)
lab_local=$(cat "$lab_root/runs/local-path.txt")
mkdir -p "$lab_local/gvisor-buildtmp" "$lab_local/gvisor-tmp" "$lab_root/downloads/apptainer-cache"
export APPTAINER_CACHEDIR="$lab_root/downloads/apptainer-cache"
export APPTAINER_TMPDIR="$lab_local/gvisor-tmp"
exec apptainer exec --userns --containall --cleanenv --no-home \
  --bind "$lab_root:/lab" --bind "$lab_local:/local" \
  --bind "$lab_local/gvisor-buildtmp:/tmp" \
  --env TMPDIR=/tmp --env GOCACHE=/local/gvisor/go-build-cache \
  --pwd /lab/sources/gvisor "$lab_root/tools/gvisor-builder.sif" \
  bazel --batch --output_user_root=/local/gvisor/bazel-container \
  "${@:-build}" --jobs=8

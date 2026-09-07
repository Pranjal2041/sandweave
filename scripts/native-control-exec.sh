#!/bin/bash
set -euo pipefail
lab_root=$(cd "$(dirname "$0")/.." && pwd -P)
lab_local=$(cat "$lab_root/runs/local-path.txt")
mkdir -p "$lab_root/runs/native-control/home"
exec apptainer exec --userns --containall --cleanenv --no-home \
    --bind "$lab_root:/lab" --bind "$lab_root/runs/native-control:/session" \
    --bind "$lab_root/runs/native-control/home:/home/pranjala" \
    --bind "$lab_root/runs/native-control/tmp:/tmp" \
    --pwd /session "$lab_local/native-root" "$@"

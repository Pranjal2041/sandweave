#!/bin/bash
# Isolated Steam downloads using the updated 64-bit client; no host install.
set -euo pipefail
lab_root=$(cd "$(dirname "$0")/.." && pwd -P)
mkdir -p "$lab_root/tools/steamcmd-home"
exec apptainer exec --userns --contain --ipc --cleanenv \
  --home "$lab_root/tools/steamcmd-home:/root" \
  --bind "$lab_root:/lab" \
  --bind "$lab_root/tools/steamcmd-etc:/etc" \
  --pwd /lab/tools/steamcmd "$lab_root/tools/debian-trixie.sif" \
  env STEAM_PLATFORM=linux64 SSL_CERT_FILE=/lab/tools/steamcmd-certs.pem \
  /lab/tools/steamcmd/steamcmd.sh "$@"

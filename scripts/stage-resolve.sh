#!/bin/bash
set -euo pipefail
lab_root=$(cd "$(dirname "$0")/.." && pwd -P)
cd "$lab_root"
archive=${1:-downloads/DaVinci_Resolve_21.0.4_Linux.zip}
archive_sha=d0bbc5bc09aaecaa693e22b2a037f42da8c984a5e9f3e1ff77c7db681325b50f
destination=tools/gpu/resolve-installer
if [[ ! -f "$destination/.archive-sha256" ]]; then
  echo "$archive_sha  $archive" | sha256sum -c -
  if [[ -e "$destination" ]]; then
    echo "Unmarked staging directory already exists: $destination" >&2
    exit 1
  fi
  mkdir -p tools/gpu
  staging=$(mktemp -d "$lab_root/tools/gpu/resolve-staging.XXXXXX")
  trap 'rm -rf "$staging"' EXIT
  python -m zipfile -e "$archive" "$staging"
  chmod +x "$staging/DaVinci_Resolve_21.0.4_Linux.run"
  (cd "$staging" && ./DaVinci_Resolve_21.0.4_Linux.run --appimage-extract)
  test -x "$staging/squashfs-root/installer"
  test -x "$staging/squashfs-root/bin/resolve"
  echo "$archive_sha" > "$staging/.archive-sha256"
  chmod -R a+rX "$staging"
  mv "$staging" "$destination"
  trap - EXIT
fi
[[ "$(cat "$destination/.archive-sha256")" == "$archive_sha" ]]

mkdir -p tools/gpu/probes
install -m 755 scripts/qt-semaphore-probe.py scripts/futex-wake-op-probe.py tools/gpu/probes/
if [[ ! -f tools/gpu/probes/resolve-test.mov ]]; then
  ffmpeg -hide_banner -loglevel error \
    -f lavfi -i testsrc2=size=1280x720:rate=24 \
    -f lavfi -i sine=frequency=440:sample_rate=48000 -t 5 \
    -c:v dnxhd -profile:v dnxhr_lb -pix_fmt yuv422p -c:a pcm_s16le \
    -threads 4 -f mov tools/gpu/probes/resolve-test.mov.partial
  chmod a+r tools/gpu/probes/resolve-test.mov.partial
  mv tools/gpu/probes/resolve-test.mov.partial tools/gpu/probes/resolve-test.mov
fi

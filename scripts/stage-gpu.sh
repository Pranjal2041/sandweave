#!/bin/bash
set -euo pipefail
lab_root=$(cd "$(dirname "$0")/.." && pwd -P)
cd "$lab_root"
python scripts/gvisor_gpu.py --check-device "${1:-0}" >/dev/null
mkdir -p tools/gpu downloads
if [[ ! -f tools/gpu/driver/driver.json ]]; then
  python scripts/gvisor_gpu.py --stage-driver tools/gpu/driver
fi
vgl_package=downloads/virtualgl_3.1.5_amd64.deb
vgl_sha=df3f7788ce41b182a47c0d298e5cd6d2d63579522cb41825970b7726e825485e
if [[ ! -f "$vgl_package" ]]; then
  curl -fL --retry 2 https://github.com/VirtualGL/virtualgl/releases/download/3.1.5/virtualgl_3.1.5_amd64.deb -o "$vgl_package.partial"
  mv "$vgl_package.partial" "$vgl_package"
fi
echo "$vgl_sha  $vgl_package" | sha256sum -c -
mkdir -p tools/gpu/virtualgl tools/gpu/probes
if [[ ! -f tools/gpu/virtualgl/.package-sha256 ]]; then
  ar p "$vgl_package" data.tar.xz | tar -xJ -C tools/gpu/virtualgl
  echo "$vgl_sha" > tools/gpu/virtualgl/.package-sha256
fi
[[ "$(cat tools/gpu/virtualgl/.package-sha256)" == "$vgl_sha" ]]
xcb_package=downloads/libxcb-keysyms1_0.4.0-1build3_amd64.deb
xcb_sha=1d62f96a793cc1aa0df860de3f7d83edea2fa461d97e2b3d4d7f7e7a10e55f42
if [[ ! -f "$xcb_package" ]]; then
  curl -fL --retry 2 https://archive.ubuntu.com/ubuntu/pool/main/x/xcb-util-keysyms/libxcb-keysyms1_0.4.0-1build3_amd64.deb -o "$xcb_package.partial"
  mv "$xcb_package.partial" "$xcb_package"
fi
echo "$xcb_sha  $xcb_package" | sha256sum -c -
mkdir -p tools/gpu/xcb-keysyms tools/gpu/compat
if [[ ! -f tools/gpu/xcb-keysyms/.package-sha256 ]]; then
  ar p "$xcb_package" data.tar.zst | tar --zstd -x -C tools/gpu/xcb-keysyms
  chmod -R a+rX tools/gpu/xcb-keysyms
  echo "$xcb_sha" > tools/gpu/xcb-keysyms/.package-sha256
fi
[[ "$(cat tools/gpu/xcb-keysyms/.package-sha256)" == "$xcb_sha" ]]
gcc -Wall -Wextra -Werror -O2 -fPIC -shared scripts/gpu-visual-order.c \
  -Wl,-z,defs -o tools/gpu/compat/libvisualorder.so.new -ldl -lX11
chmod a+rX tools/gpu/compat/libvisualorder.so.new
mv tools/gpu/compat/libvisualorder.so.new tools/gpu/compat/libvisualorder.so
UV_CACHE_DIR="$lab_root/downloads/uv-gpu-cache" uv pip install --link-mode=copy \
  --python-version 3.10 --python-platform x86_64-manylinux_2_35 \
  --target tools/gpu/python --index-url https://download.pytorch.org/whl/cu128 \
  --requirement notes/gpu-python-requirements.txt
cp scripts/gpu-training-probe.py scripts/gpu-device-probe.py scripts/gpu-driver-probe.py \
  scripts/gpu-glx-interop-probe.py scripts/gpu-webgl.html tools/gpu/probes/
chmod -R a+rX tools/gpu
uv pip list --target tools/gpu/python --format freeze > tools/gpu/python-packages.txt

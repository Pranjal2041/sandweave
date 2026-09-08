#!/bin/sh
# Run as sandbox root, never on the host. prepare-vr-lab.py supplies /opt/vr.
set -eu
test ! -e /dev/kvm
test -f /opt/vr/monado-source/src/xrt/compositor/main/comp_compositor.c
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  build-essential cmake ninja-build pkg-config git patch libeigen3-dev \
  glslang-tools libvulkan-dev vulkan-tools \
  libxcb-randr0-dev libx11-xcb-dev libxrandr-dev libx11-dev libxxf86vm-dev \
  libwayland-dev libudev-dev libusb-1.0-0-dev libhidapi-dev \
  libgl1-mesa-dev libegl1-mesa-dev libcjson-dev libopenxr-loader1 libopenxr-dev \
  libuvc-dev libjsoncpp-dev libsystemd-dev protobuf-compiler
cd /opt/vr/monado-source
patch --batch --forward -p1 < /opt/vr/monado-vr-lab.patch
cmake -S /opt/vr/monado-source -B /opt/vr/monado-build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/vr/monado \
  -DXRT_BUILD_DRIVER_REMOTE=ON -DXRT_BUILD_DRIVER_SIMULATED=ON \
  -DXRT_FEATURE_SERVICE_SYSTEMD=OFF -DXRT_FEATURE_STEAMVR_PLUGIN=OFF \
  -DXRT_FEATURE_OPENVR=OFF -DXRT_FEATURE_SLAM=OFF -DBUILD_TESTING=OFF \
  -DGIT_DESC=f8dfadfeaeb46df3eec17bd76b7abdf42a79108c
cmake --build /opt/vr/monado-build --parallel 8
cmake --install /opt/vr/monado-build
protoc --proto_path=/opt/vr --python_out=/opt/vr /opt/vr/monado_metrics.proto
chmod -R a+rX /opt/vr

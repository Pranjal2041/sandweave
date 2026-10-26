#!/bin/bash
# Guest-only extension to install-sandbox-base.sh. No GPU is needed for building.
set -euo pipefail
profile=${1:?missing profile}
apt-get install -y --no-install-recommends libprimus-vk1 mesa-vulkan-drivers \
    libasound2 libpulse0 libfontconfig1 libxcursor1 libxinerama1 libxi6 libxrender1 \
    libxcb-keysyms1 libx11-dev libxrandr-dev libvulkan-dev python3-protobuf
mkdir -p /opt/vr /sandweave-output/gpu/virtualgl /sandweave-output/gpu/compat
cp -a /sandweave-input/monado-source /opt/vr/monado-source
cp /sandweave-input/monado-vr-lab.patch /sandweave-input/monado_metrics.proto /opt/vr/
for name in vr-monado-build.sh vr-monado-guest.sh vr-vulkan-xvnc.sh vr-remote-input.py vr_input.py vr_stream_guest.py; do
    install -m755 "/sandweave-input/$name" "/opt/vr/$name"
done
install -m755 /sandweave-input/OpenSaber0.5.0.Linux.x86_64 /opt/vr/OpenSaber0.5.0.Linux.x86_64
printf '{"file_format_version":"1.0.0","ICD":{"library_path":"/opt/engine-gpu/driver/lib/libGLX_nvidia.so.0","api_version":"1.3.0"}}\n' > /opt/vr/vulkan.json
bash /opt/vr/vr-monado-build.sh
dpkg-deb -x /sandweave-input/virtualgl_3.1.5_amd64.deb /sandweave-output/gpu/virtualgl
cc -O2 -fPIC -shared /sandweave-input/gpu-visual-order.c -Wl,-z,defs -ldl -lX11 \
    -o /sandweave-output/gpu/compat/libvisualorder.so
if [ "$profile" = vr/opensaber-session ]; then
    # Task environments build their own Monado (plus their journals) from the lab-patched source
    # and run their own session supervisor: Xvfb, VirtualGL's vglrun, ffmpeg, numpy, and the
    # VR stack's tools at the paths that supervisor expects.
    rm -rf /opt/vr/monado-build
    touch /opt/vr/monado-patch-applied
    install -m755 /sandweave-input/vr-metrics.py /opt/vr/vr-metrics.py
    apt-get install -y --no-install-recommends xvfb ffmpeg python3-numpy git cmake ninja-build
    mkdir -p /opt/vr-native
    ln -sfn /opt/engine-gpu/virtualgl /opt/vr-native/virtualgl
    ln -sfn /opt/engine-gpu/compat/libvisualorder.so /opt/vr-native/libvisualorder.so
else
    rm -rf /opt/vr/monado-source /opt/vr/monado-build
fi

if [ -d /sandweave-input/open-saber-source ]; then
    # Journal (game state), game-time control, shader warm-up and XR probe pack for the official binary.
    build=/opt/vr/open-saber-build
    mkdir -p "$build/home"
    cp -a /sandweave-input/open-saber-source "$build/source"
    patch --batch --forward -d "$build/source" -p1 < /sandweave-input/open-saber-journal.patch
    patch --batch --forward -d "$build/source" -p1 < /sandweave-input/open-saber-time-control.patch
    patch --batch --forward -d "$build/source" -p1 < /sandweave-input/open-saber-warmup.patch
    patch --batch --forward -d "$build/source" -p1 < /sandweave-input/open-saber-xr-probe.patch
    python3 -c "import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" \
        /sandweave-input/Godot_v4.3-stable_linux.x86_64.zip "$build"
    godot="$build/Godot_v4.3-stable_linux.x86_64"
    chmod 755 "$godot"
    # The first headless pass imports textures, meshes and audio; the export packs the imported data.
    HOME="$build/home" "$godot" --headless --path "$build/source" --import
    HOME="$build/home" "$godot" --headless --path "$build/source" --export-pack Linux/X11 /opt/vr/open_saber_journal.pck
    test "$(stat -c %s /opt/vr/open_saber_journal.pck)" -gt 5000000
    rm -rf "$build"
fi

if [[ "$profile" == *gunspinning* ]]; then
    mkdir -p /sandweave-output/gpu/vr /opt/gunspinning-lab
    cp -a /sandweave-input/gunspinning-linux-2.0.1 /sandweave-input/xrizer-v0.5 /sandweave-output/gpu/vr/
    cp -a /sandweave-input/gunspinning-primus-source /sandweave-output/gpu/vr/
    patch --batch --forward -d /sandweave-output/gpu/vr/gunspinning-primus-source -p1 \
        < /sandweave-input/primus-vk-visible-rows.patch
    cc -shared -fPIC -O2 -Wall -Wextra -Werror -Wl,-Bsymbolic \
        -I /sandweave-input /sandweave-input/sdl-gamepad-proxy.c -ldl \
        -o /sandweave-output/gpu/vr/libsdl-gamepad-proxy.so
    install -m755 /sandweave-input/gamepad-input.py /opt/gunspinning-lab/gamepad-input.py
fi
chmod -R a+rX /opt/vr /sandweave-output/gpu

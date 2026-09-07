#!/bin/sh
set -eu
test "$(id -u)" = 0
source=/opt/engine-gpu/resolve-installer/squashfs-root
test -x "$source/installer"
test -x /usr/local/bin/engine-resolve

DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  libapr1 libaprutil1 libxcb-composite0 libxcb-cursor0 libxcb-damage0 \
  libxcb-xinput0 libxcb-xinerama0 libxcb-icccm4 libxcb-image0 \
  libxcb-render-util0 libxcb-keysyms1 ocl-icd-libopencl1 \
  libasound2-plugins pipewire-pulse pulseaudio-utils
# The desktop has no physical ALSA card. Route ALSA to its PipeWire server;
# PipeWire provides an automatic virtual sink when no hardware is available.
ln -sfn 99-pulseaudio-default.conf.example \
  /etc/alsa/conf.d/99-pulseaudio-default.conf
systemctl --global enable pipewire-pulse.socket
QT_QPA_PLATFORM=offscreen "$source/AppRun" -i -y -a
test -x /opt/resolve/bin/resolve

# Resolve 21 writes these locations under its installation tree as the user.
install -d -o ga -g ga -m 755 '/opt/resolve/Apple Immersive/Calibration' \
  /opt/resolve/Extras /opt/resolve/logs
install -d -o ga -g ga /home/ga/Videos
install -o ga -g ga -m 644 /opt/engine-gpu/probes/resolve-test.mov \
  /home/ga/Videos/resolve-test.mov

mkdir -p /usr/local/share/applications
sed 's|^Exec=.*|Exec=/usr/local/bin/engine-resolve %u|' \
  /usr/share/applications/com.blackmagicdesign.resolve.desktop \
  > /usr/local/share/applications/com.blackmagicdesign.resolve.desktop

#!/bin/sh
set -eu
gpu_root=/opt/engine-gpu
test -r "$gpu_root/driver/lib/libnvidia-ml.so.1"
test -x "$gpu_root/driver/bin/nvidia-smi"

# Prefer the host-matched driver even after guest packages run ldconfig.
mkdir -p /etc/ld.so.conf.d /usr/local/bin
printf '%s\n' "$gpu_root/driver/lib" > /etc/ld.so.conf.d/00-engine-nvidia.conf
/sbin/ldconfig -X
ln -sfn "$gpu_root/driver/bin/nvidia-smi" /usr/local/bin/nvidia-smi

mkdir -p /usr/local/share/applications
cat > /usr/local/share/applications/engine-google-earth-gpu.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Google Earth Pro (GPU)
TryExec=/usr/bin/google-earth-pro
Exec=/usr/local/bin/engine-gpu-gl /usr/bin/google-earth-pro %F
Icon=google-earth-pro
Terminal=false
Categories=Education;Science;Geography;
EOF

exec "$@"

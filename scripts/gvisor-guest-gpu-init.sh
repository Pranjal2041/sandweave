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

exec "$@"

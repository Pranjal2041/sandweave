#!/bin/sh
set -eu
gpu_root=/opt/engine-gpu
export LD_LIBRARY_PATH="$gpu_root/driver/lib:$gpu_root/virtualgl/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PATH="$gpu_root/driver/bin:$gpu_root/virtualgl/opt/VirtualGL/bin:$PATH"
export PYTHONPATH="$gpu_root/python${PYTHONPATH:+:$PYTHONPATH}"
export __EGL_VENDOR_LIBRARY_FILENAMES="$gpu_root/driver/egl.json"
export VK_DRIVER_FILES="$gpu_root/driver/vulkan.json"
export VK_ICD_FILENAMES="$VK_DRIVER_FILES"
exec "$@"

#!/bin/sh
# Run inside a GPU sandbox prepared by prepare-vr-lab.py.
# Requires guest packages libprimus-vk1 and mesa-vulkan-drivers.
set -eu
test -f /opt/vr/vulkan.json
test -f /usr/share/vulkan/icd.d/lvp_icd.x86_64.json
exec /usr/local/bin/engine-gpu env \
  VK_DRIVER_FILES=/opt/vr/vulkan.json:/usr/share/vulkan/icd.d/lvp_icd.x86_64.json \
  VK_ICD_FILENAMES=/opt/vr/vulkan.json:/usr/share/vulkan/icd.d/lvp_icd.x86_64.json \
  ENABLE_PRIMUS_LAYER=1 PRIMUS_VK_RENDERID=10de:0 PRIMUS_VK_DISPLAYID=10005:0 \
  "$@"

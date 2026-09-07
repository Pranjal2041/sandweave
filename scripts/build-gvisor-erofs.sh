#!/bin/bash
set -euo pipefail
lab_root=$(cd "$(dirname "$0")/.." && pwd -P)
lab_local=$(cat "$lab_root/runs/local-path.txt")
image="$lab_local/images/base-aff6d6d64a8a7dc7108f7f3a02ff3727a05410f1e8524b79a5b53a9d1a04959e.ext4"
source_dir="$lab_local/gvisor-ready-ro"
output="$lab_local/gvisor/ubuntu-ready.erofs"
mountpoint -q "$source_dir"
gcc -shared -fPIC -O2 -Wall -Wextra -Werror \
  "$lab_root/scripts/ext4-xattr-reader.c" -o "$lab_root/tools/ext4-xattr-reader.so" -ldl
LD_PRELOAD="$lab_root/tools/ext4-xattr-reader.so" \
  GVM_EXT4_IMAGE="$image" GVM_EXT4_MOUNT="$source_dir" \
  "$lab_root/tools/erofs-native/usr/bin/mkfs.erofs" --quiet -E noinline_data \
  "$output.tmp" "$source_dir"
mv "$output.tmp" "$output"

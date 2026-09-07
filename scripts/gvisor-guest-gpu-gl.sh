#!/bin/sh
set -eu
export LD_PRELOAD="/opt/engine-gpu/compat/libvisualorder.so${LD_PRELOAD:+:$LD_PRELOAD}"
exec /usr/local/bin/engine-gpu vglrun -d egl0 "$@"

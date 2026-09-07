#!/bin/sh
set -eu
cd /opt/resolve
exec /usr/local/bin/engine-gpu-gl -nodl /opt/resolve/bin/resolve "$@"

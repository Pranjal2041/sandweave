#!/bin/bash
# Private native comparison, invoked by run-native-gpu-app.py with a prepared root.
set -euo pipefail
test -d /session/runtime
test -d /opt/alyx/game
export HOME=/home/ga XDG_RUNTIME_DIR=/session/runtime
export XDG_CONFIG_HOME=/session/config
export WINEPREFIX=/home/ga/.local/share/alyx-wine
export WINEDEBUG=-all,err+all,+seh,+loaddll
export WINEDLLOVERRIDES='mscoree,mshtml=;dxgi,d3d11,d3d9,d3d10core=n'
export XR_RUNTIME_JSON=/opt/vr/monado/share/openxr/1/openxr_monado.json
export PROTON_VR_RUNTIME=/opt/engine-gpu/vr/xrizer-v0.5
export DXVK_CONFIG='dxvk.enableGraphicsPipelineLibrary = False'
export VK_DRIVER_FILES=/opt/vr/vulkan.json VK_ICD_FILENAMES=/opt/vr/vulkan.json
export P_OVERRIDE_ACTIVE_CONFIG=remote XRT_NO_STDIN=1
export XRT_COMPOSITOR_FORCE_TARGET=debug_image XRT_COMPOSITOR_DEFAULT_FRAMERATE=90
export XRT_METRICS_FILE=/session/monado-metrics.pb XRT_METRICS_EARLY_FLUSH=1
export XRT_LAB_EYE_CAPTURE=/session/monado-eye.ppm
python3 - <<'PY'
import json, socket, shutil
from pathlib import Path
root = Path('/session/config')
(root/'monado').mkdir(parents=True, exist_ok=True)
(root/'openvr').mkdir(parents=True, exist_ok=True)
shutil.copy2('/home/ga/.config/openvr/openvrpaths.vrpath', root/'openvr/openvrpaths.vrpath')
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
(root/'monado/config_v0.json').write_text(json.dumps({
    'remote': {'version': 0, 'port': port, 'view_count': 2}})+'\n')
Path('/session/input-port.txt').write_text(str(port)+'\n')
PY
wine=/opt/engine-gpu/vr/GE-Proton9-27/files/bin/wine64
server=/opt/engine-gpu/vr/GE-Proton9-27/files/bin/wineserver
mono_pid=
cleanup() {
    "$server" -k || true
    if [[ -n "$mono_pid" ]]; then
        kill "$mono_pid" 2>/dev/null || true
        wait "$mono_pid" 2>/dev/null || true
    fi
}
trap cleanup EXIT
/opt/vr/monado/bin/monado-service > /session/monado.log 2>&1 &
mono_pid=$!
ready=false
for attempt in $(seq 1 30); do
    kill -0 "$mono_pid"
    if grep -q 'Supported formats:' /session/monado.log; then ready=true; break; fi
    sleep 1
done
[[ "$ready" == true ]]
cd /opt/alyx/game
timeout -k 3 900 sh /opt/vr/vr-vulkan-xvnc.sh "$wine" bin/win64/hlvr.exe \
    -vr -noasserts -nopassiveasserts -console -condebug "$@"

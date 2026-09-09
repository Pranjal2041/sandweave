#!/bin/sh
set -eu
test -x /opt/engine-gpu/vr/gunspinning-linux-2.0.1/GunSpinningVR
test -f /opt/engine-gpu/vr/xrizer-v0.5/bin/linux64/vrclient.so
test -f /opt/vr/vr-vulkan-xvnc.sh
mkdir -p /opt/gunspinning-lab/primus-build
install -m755 /opt/engine-gpu/vr/libsdl-gamepad-proxy.so /opt/gunspinning-lab/libsdl-gamepad-proxy.so
cp -a /opt/engine-gpu/vr/gunspinning-primus-source/. /opt/gunspinning-lab/primus-build/
chmod -R u+w /opt/gunspinning-lab/primus-build
touch /opt/gunspinning-lab/primus-build/primus_vk.cpp
make -C /opt/gunspinning-lab/primus-build -j2 libprimus_vk.so CXXFLAGS=-O2
install -m755 /opt/gunspinning-lab/primus-build/libprimus_vk.so /opt/gunspinning-lab/libprimus_vk.so
python3 - <<'PY'
import json
from pathlib import Path
path = Path('/usr/share/vulkan/implicit_layer.d/primus_vk.json')
data = json.loads(path.read_text())
data['layer']['library_path'] = '/opt/gunspinning-lab/libprimus_vk.so'
path.write_text(json.dumps(data) + '\n')
path = Path('/home/ga/.config/openvr/openvrpaths.vrpath')
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(dict(version=1, jsonid='vrpathreg',
    runtime=['/opt/engine-gpu/vr/xrizer-v0.5'], config=[], log=[], external_drivers=[])) + '\n')
PY
chown -R ga:ga /home/ga/.config/openvr

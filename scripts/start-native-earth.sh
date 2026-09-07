#!/bin/bash
set -euo pipefail
lab_root=$(cd "$(dirname "$0")/.." && pwd -P)
exec 9>"$lab_root/runs/native-control/instance.lock"
flock -n 9
python3 "$lab_root/scripts/prepare-native-display.py"
exec "$lab_root/scripts/native-control-exec.sh" env LAB_RENDER_THREADS="${LAB_RENDER_THREADS:-1}" bash -c '
set -euo pipefail
test ! -e /dev/kvm
id
uname -r
export DISPLAY=:98 XDG_RUNTIME_DIR=/session/runtime XDG_CONFIG_HOME=/session/config XDG_CACHE_HOME=/session/cache LP_NUM_THREADS="$LAB_RENDER_THREADS" MOZ_ENABLE_WAYLAND=0
mkdir -p "$XDG_RUNTIME_DIR" /session/firefox-profile
chmod 700 "$XDG_RUNTIME_DIR"
printf "labvnc01\n" | vncpasswd -f > /session/vnc.passwd
chmod 600 /session/vnc.passwd
Xtigervnc :98 -geometry 1280x800 -depth 24 -localhost yes -rfbport 25902 -SecurityTypes VncAuth -rfbauth /session/vnc.passwd -ac -nolisten tcp > /session/xvnc.log 2>&1 &
xvnc_pid=$!
python3 -m http.server 28080 --bind 127.0.0.1 --directory /session/www > /session/http.log 2>&1 &
http_pid=$!
cleanup() {
    kill "$xvnc_pid" "$http_pid" 2>/dev/null || true
    wait "$xvnc_pid" "$http_pid" 2>/dev/null || true
}
trap cleanup EXIT
for attempt in $(seq 1 30); do if xdpyinfo >/dev/null 2>&1; then break; fi; kill -0 "$xvnc_pid"; sleep 1; done
xdpyinfo >/dev/null
dbus-run-session -- bash /lab/scripts/native-earth-session.sh
'

#!/bin/bash
set -euxo pipefail
install -d -o ga -g ga /home/ga/firefox-profile
runuser -u ga -- env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus systemd-run --user --unit=lab-firefox --collect --setenv=DISPLAY=:1 --setenv=XAUTHORITY=/home/ga/.Xauthority --setenv=MOZ_ENABLE_WAYLAND=0 /opt/firefox/firefox --no-remote --profile /home/ga/firefox-profile http://127.0.0.1:8080/
export DISPLAY=:1 XAUTHORITY=/home/ga/.Xauthority
window=''
for attempt in $(seq 1 60); do
    window=$(xdotool search --onlyvisible --class firefox 2>/dev/null | head -1 || true)
    if [ -n "$window" ]; then break; fi
    sleep 1
done
test -n "$window"
xdotool windowactivate --sync "$window"
xdotool key --clearmodifiers ctrl+l
xdotool type --clearmodifiers --delay 30 'http://127.0.0.1:8080/'
xdotool key Return
sleep 3
xdotool getactivewindow getwindowname
import -window root /tmp/desktop-firefox.png
pgrep -a gnome-shell
/opt/firefox/firefox --version
glxinfo -B
printf 'DESKTOP_TEST_COMPLETED\n'

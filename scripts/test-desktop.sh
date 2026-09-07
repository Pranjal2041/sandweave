#!/bin/bash
set -euxo pipefail
export DISPLAY=:1 XAUTHORITY=/home/ga/.Xauthority
rm -f /tmp/lab-desktop-input.txt
runuser -u ga -- env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus DISPLAY=:1 XAUTHORITY=/home/ga/.Xauthority gnome-terminal --title='UML verification'
window=''
for attempt in $(seq 1 30); do
    window=$(xdotool search --onlyvisible --class Gnome-terminal 2>/dev/null | tail -1 || true)
    if [ -n "$window" ]; then break; fi
    sleep 1
done
test -n "$window"
timeout 10s xdotool windowactivate --sync "$window"
xdotool key --clearmodifiers Escape
sleep 1
timeout 10s xdotool windowfocus --sync "$window"
sleep 1
xdotool type --clearmodifiers --delay 50 'clear; echo "UML Linux: KVM absent, full guest privileges"; uname -r; id; sudo -n id; test ! -e /dev/kvm && echo KVM_ABSENT; printf "DESKTOP_INPUT_PASS\n" | tee /tmp/lab-desktop-input.txt'
xdotool key Return
for attempt in $(seq 1 20); do
    if [ -f /tmp/lab-desktop-input.txt ]; then break; fi
    sleep 1
done
grep -x DESKTOP_INPUT_PASS /tmp/lab-desktop-input.txt
xdotool getwindowfocus getwindowname
sleep 2
import -window root /tmp/desktop-terminal.png
timeout 20s glxinfo -B
printf 'DESKTOP_INPUT_TEST_PASS\n'

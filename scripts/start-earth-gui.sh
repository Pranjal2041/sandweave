#!/bin/bash
set -euxo pipefail
render_threads=${LAB_RENDER_THREADS:-1}
[[ "$render_threads" =~ ^[0-9]+$ ]]
runuser -u ga -- env XDG_RUNTIME_DIR=/run/user/1000 \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
  systemd-run --user --unit=lab-earth --collect \
  --setenv=DISPLAY=:1 --setenv=XAUTHORITY=/home/ga/.Xauthority \
  --setenv=LP_NUM_THREADS="$render_threads" /usr/bin/google-earth-pro

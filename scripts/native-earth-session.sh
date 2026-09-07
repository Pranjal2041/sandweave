#!/bin/bash
set -euo pipefail
metacity --replace --no-composite > /session/metacity.log 2>&1 &
wm_pid=$!
trap 'kill "$wm_pid" 2>/dev/null || true' EXIT
/usr/bin/google-earth-pro > /session/earth.log 2>&1

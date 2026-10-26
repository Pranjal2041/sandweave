#!/bin/sh
# Experimental Monado/Open Saber launch commands, executed inside a GPU sandbox.
set -eu
export HOME=/home/ga XDG_RUNTIME_DIR=/run/user/1000
export VK_DRIVER_FILES=/opt/vr/vulkan.json
export VK_ICD_FILENAMES=$VK_DRIVER_FILES
case "${1:-}" in
  monado)
    export P_OVERRIDE_ACTIVE_CONFIG=remote XRT_NO_STDIN=1
    export XRT_COMPOSITOR_FORCE_TARGET=debug_image
    export XRT_COMPOSITOR_DEFAULT_FRAMERATE=${VR_HZ:-90}
    export XRT_METRICS_FILE=/tmp/monado-metrics.pb XRT_METRICS_EARLY_FLUSH=1
    export XRT_LAB_EYE_CAPTURE=/tmp/monado-eye.ppm
    exec /opt/vr/monado/bin/monado-service
    ;;
  game)
    export DISPLAY=${VR_X11_DISPLAY:-:1} XAUTHORITY=${VR_XAUTHORITY:-/home/ga/.Xauthority}
    export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
    export XR_RUNTIME_JSON=/opt/vr/monado/share/openxr/1/openxr_monado.json
    export OXR_LAB_ALLOW_MISSING_GLX_CONFIG=1
    shift
    pack=/opt/vr/open_saber_journal.pck
    if [ -f "$pack" ]; then
      # Gameplay journal (one generation per launch; the game reads and deletes the
      # HMAC key) and the game-time control file, under the user's runtime directory.
      state=$XDG_RUNTIME_DIR/open-saber
      mkdir -p "$state"
      n=1
      while [ -e "$state/journal-$(printf %02d $n).jsonl" ]; do n=$((n + 1)); done
      generation=$(printf %02d $n)
      head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$state/journal-$generation.key"
      export OPEN_SABER_JOURNAL=$state/journal-$generation.jsonl OPEN_SABER_JOURNAL_KEY=$state/journal-$generation.key
      export SANDWEAVE_TIME_CONTROL=$state/time-control.json
      set -- --main-pack "$pack" "$@"
    fi
    exec /opt/vr/OpenSaber0.5.0.Linux.x86_64 --rendering-method gl_compatibility \
      --rendering-driver opengl3 --print-fps --resolution 1280x720 "$@"
    ;;
  *) echo 'usage: vr-monado-guest.sh monado|game [game options]' >&2; exit 2 ;;
esac

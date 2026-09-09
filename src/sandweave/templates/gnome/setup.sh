#!/bin/sh
set -eu
# The lab's prepared image has a oneshot unit which forks a long-lived daemon.
# systemd then spends its 90-second stop timeout killing that daemon at boot.
# Run the secrets service in the foreground, with its actual D-Bus readiness.
if test -f /usr/lib/systemd/user/gnome-keyring.service; then
    mkdir -p /etc/systemd/user/gnome-keyring.service.d
    cat > /etc/systemd/user/gnome-keyring.service.d/sandweave.conf <<'EOF'
[Service]
Type=dbus
BusName=org.freedesktop.secrets
ExecStart=
ExecStart=/usr/bin/gnome-keyring-daemon --foreground --components=pkcs11,secrets
TimeoutStopSec=5
EOF
    if test -S /run/user/1000/bus; then
        runuser -u ga -- env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
            systemctl --user daemon-reload
        runuser -u ga -- env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
            systemctl --user restart --no-block gnome-keyring.service
    fi
fi

#!/bin/sh
# Configure the guest before systemd can start the desktop's services.
set -eu
umask 022

# gVisor supplies the desktop's virtual devices; it has no host uevent socket
# for udev to consume. A restarting udev job can indefinitely hold sysinit.target
# when each failed attempt takes longer than systemd's start-limit interval.
# Disable physical-device discovery before systemd starts, without relying on
# how quickly those unsupported services fail.
mkdir -p /etc/systemd/system
for unit in systemd-udevd.service systemd-udevd-control.socket systemd-udevd-kernel.socket \
            systemd-udev-trigger.service systemd-udev-settle.service; do
    ln -sfn /dev/null "/etc/systemd/system/$unit"
done

# These local D-Bus services only need AF_UNIX. Their additional private
# network namespace currently fails during systemd's setup under gVisor.
# Keep their address-family restriction and other service hardening intact.
for unit in accounts-daemon systemd-hostnamed systemd-localed; do
    mkdir -p "/etc/systemd/system/$unit.service.d"
    cat > "/etc/systemd/system/$unit.service.d/sandweave.conf" <<'EOF'
[Service]
PrivateNetwork=no
EOF
done

# Older prepared images start a persistent daemon from a oneshot unit.
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
fi

# -iconic still creates a window in GNOME's overview. -nowin retains the
# clipboard helper without displaying a configuration window.
for session in /etc/X11/Xtigervnc-session /etc/X11/Xvnc-session; do
    if test -f "$session"; then
        sed -i -E '/^[[:space:]]*(tiger)?vncconfig[[:space:]]/s/-iconic/-nowin/g' "$session"
    fi
done

# This session deliberately uses Xvnc instead of GDM. Acknowledge GNOME's
# one-time notice about its unavailable GDM lock screen before the first login.
# Other application and system notifications remain enabled.
install -d -o ga -g ga -m 700 /home/ga/.local /home/ga/.local/share /home/ga/.local/share/gnome-shell
touch /home/ga/.local/share/gnome-shell/lock-warning-shown
chown ga:ga /home/ga/.local/share/gnome-shell/lock-warning-shown

exec /sbin/init "$@"

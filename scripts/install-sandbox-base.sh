#!/bin/bash
# Runs as guest root under gVisor during setup, never on the host.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
profile=${1:?missing installation profile}
test -d /sandweave-input
test -d /sandweave-output
umask 022
mkdir -p /usr/sbin /workspace /etc
# Package postinst scripts must not start services while creating the image.
printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
chmod 755 /usr/sbin/policy-rc.d
cat > /etc/apt/sources.list <<'EOF'
deb http://archive.ubuntu.com/ubuntu jammy main restricted universe multiverse
deb http://archive.ubuntu.com/ubuntu jammy-updates main restricted universe multiverse
deb http://security.ubuntu.com/ubuntu jammy-security main restricted universe multiverse
EOF
apt-get update
apt-get install -y --no-install-recommends python3 python-is-python3 python3-pip \
    python3-venv bash ca-certificates curl git tar gzip xz-utils unzip \
    coreutils procps iproute2 iputils-ping util-linux sudo locales \
    systemd systemd-sysv dbus dbus-user-session build-essential
mkdir -p /etc/systemd/system
ln -sfn /dev/null /etc/systemd/system/apt-daily.service
ln -sfn /dev/null /etc/systemd/system/apt-daily-upgrade.service
ln -sfn /dev/null /etc/systemd/system/systemd-resolved.service
: > /etc/fstab
: > /etc/machine-id
mkdir -p /var/lib/dbus
ln -sfn /etc/machine-id /var/lib/dbus/machine-id

if [ "$profile" = docker ]; then
    apt-get install -y --no-install-recommends docker.io iptables
    mkdir -p /etc/docker
    printf '{"storage-driver":"overlay2"}\n' > /etc/docker/daemon.json
fi

case "$profile" in
    gnome|vr/*|games/*)
        apt-get install -y --no-install-recommends gnome-session gnome-shell \
            gnome-terminal gnome-keyring dbus-x11 tigervnc-standalone-server \
            tigervnc-common tigervnc-tools x11-utils x11-xserver-utils xdotool \
            fonts-dejavu-core mesa-utils gnome-control-center accountsservice \
            at-spi2-core librsvg2-common adwaita-icon-theme-full ibus colord \
            gnome-backgrounds gjs python3-gi gir1.2-gtk-3.0 \
            libxcb1-dev libxcb-shm0-dev libxcb-xtest0-dev
        if ! id ga >/dev/null 2>&1; then
            useradd -m -u 1000 -s /bin/bash ga
        fi
        printf 'ga ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/sandweave-ga
        chmod 440 /etc/sudoers.d/sandweave-ga
        mkdir -p /var/lib/systemd/linger
        touch /var/lib/systemd/linger/ga
        install -d -o ga -g ga -m 700 /home/ga/.vnc
        # The SDK transports screenshots and input over its authenticated agent.
        # This password only serves the optional loopback VNC endpoint.
        python3 -c 'import secrets; print(secrets.token_urlsafe(12))' > /etc/sandweave-vnc-password
        chmod 600 /etc/sandweave-vnc-password
        tigervncpasswd -f < /etc/sandweave-vnc-password > /home/ga/.vnc/passwd
        chown ga:ga /home/ga/.vnc/passwd
        chmod 600 /home/ga/.vnc/passwd
        mkdir -p /etc/tigervnc
        printf ':1=ga\n' > /etc/tigervnc/vncserver.users
        printf 'session=gnome\ngeometry=1920x1080\ndepth=24\nlocalhost=no\nalwaysshared\nsecuritytypes=vncauth\n' > /home/ga/.vnc/config
        chown ga:ga /home/ga/.vnc/config
        systemctl enable tigervncserver@:1.service
        mkdir -p /sandweave-output/fast-io
        cc -std=c11 -O3 -Wall -Wextra -Werror /sandweave-input/xvnc-fast-io.c \
            -o /sandweave-output/fast-io/bridge -lxcb -lxcb-shm -lxcb-xtest
        cp -L /usr/lib/x86_64-linux-gnu/libxcb-xtest.so.0 /sandweave-output/fast-io/
        ;;
esac

case "$profile" in
    vr/*|games/*)
        bash /sandweave-input/install-vr.sh "$profile"
        ;;
esac
rm /usr/sbin/policy-rc.d
apt-get clean
rm -rf /var/lib/apt/lists/*
# A regular tar retains guest ownership and xattrs without host chown or mounts.
# Nothing from the input/output bind mounts is included in the image.
tar --numeric-owner --xattrs --acls --one-file-system \
    --exclude=./proc --exclude=./sys --exclude=./dev --exclude=./run --exclude=./tmp \
    --exclude=./sandweave-input --exclude=./sandweave-output \
    -cpf /sandweave-output/rootfs.tar -C / .
python3 - <<'PY'
import tarfile
with tarfile.open('/sandweave-output/rootfs.tar', 'a') as archive:
    for name in ('proc', 'sys', 'dev', 'run', 'tmp'):
        entry = tarfile.TarInfo('./' + name)
        entry.type = tarfile.DIRTYPE
        entry.mode = 0o1777 if name == 'tmp' else 0o755
        archive.addfile(entry)
PY

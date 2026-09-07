#!/bin/bash
set -euxo pipefail
touch /etc/cloud/cloud-init.disabled
printf '/dev/ubda / ext4 defaults 0 1\n' > /etc/fstab
mkdir -p /etc/systemd/network
cat > /etc/systemd/network/10-uml.network <<'NETWORK'
[Match]
Name=vec0
[Network]
Address=10.0.2.15/24
Gateway=10.0.2.2
DNS=10.0.2.3
NETWORK
systemctl enable systemd-networkd ssh
systemctl mask apt-daily.service apt-daily-upgrade.service
mkdir -p /etc/systemd/system/getty@tty0.service.d
cat > /etc/systemd/system/getty@tty0.service.d/autologin.conf <<'GETTY'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --noclear %I linux
GETTY
cp /mnt/lab/scripts/guest-systemd-init.sh /lab-init
chmod +x /lab-init
sync

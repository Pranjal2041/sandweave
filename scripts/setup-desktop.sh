#!/bin/bash
set -euxo pipefail
if ! id ga >/dev/null 2>&1; then useradd -m -u 1000 -s /bin/bash ga; fi
usermod -aG sudo,docker ga
printf 'ga ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/lab-ga
chmod 440 /etc/sudoers.d/lab-ga
sudo -u ga sudo -n id
mkdir -p /opt
tar -xf /mnt/lab/downloads/firefox.tar.xz -C /opt
ln -sf /opt/firefox/firefox /usr/local/bin/firefox
loginctl enable-linger ga
install -d -o ga -g ga -m 700 /home/ga/.vnc
printf 'labvnc01\n' | tigervncpasswd -f > /home/ga/.vnc/passwd
chown ga:ga /home/ga/.vnc/passwd
chmod 600 /home/ga/.vnc/passwd
bash /mnt/lab/scripts/configure-vnc-service.sh
printf 'DESKTOP_SERVER_STARTED\n'

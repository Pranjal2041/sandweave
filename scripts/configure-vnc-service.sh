#!/bin/bash
set -euxo pipefail
printf ':1=ga\n' > /etc/tigervnc/vncserver.users
if [ -f /home/ga/.vnc/xstartup ]; then mv /home/ga/.vnc/xstartup /home/ga/.vnc/xstartup.initial; fi
cat > /home/ga/.vnc/tigervnc.conf <<'CONFIG'
$session = "gnome";
$geometry = "1920x1080";
$depth = "24";
$localhost = "no";
$SecurityTypes = "VncAuth";
CONFIG
chown ga:ga /home/ga/.vnc/tigervnc.conf
systemctl enable tigervncserver@:1.service
systemctl start tigervncserver@:1.service
systemctl status tigervncserver@:1.service --no-pager

#!/bin/bash
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
apt-get install -y --no-install-recommends gnome-session gnome-shell gnome-terminal dbus-x11 tigervnc-standalone-server tigervnc-common tigervnc-tools x11-utils x11-xserver-utils xdotool imagemagick fonts-dejavu-core mesa-utils gnome-control-center accountsservice network-manager at-spi2-core librsvg2-common adwaita-icon-theme-full ibus dbus-user-session colord gnome-backgrounds gjs

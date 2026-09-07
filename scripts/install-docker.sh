#!/bin/bash
set -euxo pipefail
test "$(cat /proc/1/comm)" = systemd
test ! -e /dev/kvm
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
apt-get update
apt-get install -y --no-install-recommends docker.io docker-compose sysbench
systemctl enable --now docker
systemctl is-active docker
docker info

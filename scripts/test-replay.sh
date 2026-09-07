#!/bin/bash
set -euxo pipefail
uname -a
nproc
test ! -e /dev/kvm
test "$(cat /proc/1/comm)" = systemd
/mnt/lab/tools/seccomp-trap
systemctl is-active docker
cd /root/lab-compose
docker compose up -d --wait --wait-timeout 120
result=$(docker compose exec -T db mariadb -N -uroot -plab-only-password -e 'SELECT value FROM lab.probe WHERE id=1;')
test "$result" = persistent-under-uml
printf 'REPLAY_VOLUME_PERSISTENCE_PASS\n'
bash /mnt/lab/scripts/test-firefox-headless.sh
systemd-analyze
printf 'REPLAY_TEST_PASS\n'

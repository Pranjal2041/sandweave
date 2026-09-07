#!/bin/bash
set -euxo pipefail
mkdir -p /usr/local/lib/docker/cli-plugins /root/lab-compose
install -m 755 /mnt/lab/downloads/docker-compose-linux-x86_64 /usr/local/lib/docker/cli-plugins/docker-compose
cp /mnt/lab/scripts/compose.yaml /root/lab-compose/compose.yaml
cd /root/lab-compose
docker compose down --volumes --remove-orphans
test "$(cat /proc/1/comm)" = systemd
test ! -e /dev/kvm
systemctl reset-failed docker
systemctl start docker
docker info
docker compose version
SECONDS=0
docker compose pull
printf 'IMAGE_PULL_SECONDS=%s\n' "$SECONDS"
SECONDS=0
docker compose up -d --wait --wait-timeout 180
printf 'FIRST_START_SECONDS=%s\n' "$SECONDS"
curl -fsS http://127.0.0.1:8080/ | head -5
docker run --rm --network uml-lab_default nginx:1.28-alpine wget -q -O- http://web/ | head -5
docker compose exec -T db mariadb -uroot -plab-only-password -e "CREATE DATABASE lab; CREATE TABLE lab.probe (id INT PRIMARY KEY, value VARCHAR(64)); INSERT INTO lab.probe VALUES (1, 'persistent-under-uml'); SELECT * FROM lab.probe;"
docker run --rm --memory 128m --cpus 0.5 nginx:1.28-alpine sh -c 'cat /sys/fs/cgroup/memory.max; cat /sys/fs/cgroup/cpu.max'
docker run --privileged --rm nginx:1.28-alpine sh -c 'mkdir /tmp/mount-test; mount -t tmpfs tmpfs /tmp/mount-test; touch /tmp/mount-test/works; stat -f -c %T /tmp/mount-test'
docker compose down
SECONDS=0
docker compose up -d --wait --wait-timeout 120
printf 'WARM_START_SECONDS=%s\n' "$SECONDS"
result=$(docker compose exec -T db mariadb -N -uroot -plab-only-password -e 'SELECT value FROM lab.probe WHERE id=1;')
test "$result" = persistent-under-uml
printf 'VOLUME_PERSISTENCE=PASS\n'
iptables -t nat -S
for image in mariadb:10.11 nginx:1.28-alpine; do docker image inspect "$image" --format '{{json .RepoDigests}}'; done
printf 'DOCKER_TESTS_PASS\n'

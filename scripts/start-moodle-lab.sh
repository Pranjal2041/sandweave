#!/bin/bash
set -euxo pipefail
for volume in general-vm-moodle-docker general-vm-moodle-containerd general-vm-moodle-data general-vm-moodle-config; do docker volume create "$volume"; done
if ! docker inspect general-vm-moodle >/dev/null 2>&1; then
    docker run -d --name general-vm-moodle --privileged --cgroupns=private --restart unless-stopped \
      --tmpfs /run --tmpfs /run/lock --tmpfs /tmp \
      -v general-vm-moodle-docker:/var/lib/docker -v general-vm-moodle-containerd:/var/lib/containerd -v general-vm-moodle-data:/var/moodledata \
      -v general-vm-moodle-config:/etc/moodle -p 80:80 general-vm-moodle:4.5.13
else
    docker start general-vm-moodle
fi
ready=0
for attempt in $(seq 1 90); do
    if docker exec general-vm-moodle docker info > /root/moodle-inner-docker-info.txt 2>&1; then ready=1; break; fi
    sleep 2
done
if [ "$ready" != 1 ]; then docker logs general-vm-moodle; exit 1; fi
cat /root/moodle-inner-docker-info.txt
docker exec general-vm-moodle chmod 644 /etc/php/8.1/mods-available/lab.ini /etc/apache2/sites-available/moodle.conf
docker exec general-vm-moodle systemctl --no-pager status apache2 docker
docker exec general-vm-moodle bash -c 'sudo -u ga sudo -n id; test ! -e /dev/kvm; test -w /sys/fs/cgroup/cgroup.procs'
if ! docker exec general-vm-moodle docker inspect moodle-mariadb >/dev/null 2>&1; then
    docker exec general-vm-moodle docker run -d --name moodle-mariadb --restart unless-stopped \
      -e MARIADB_ROOT_PASSWORD=rootpass -e MARIADB_DATABASE=moodle \
      -e MARIADB_USER=moodleuser -e MARIADB_PASSWORD=moodlepass \
      -v moodle-db:/var/lib/mysql -p 127.0.0.1:3306:3306 mariadb:10.11 \
      --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci
else
    docker exec general-vm-moodle docker start moodle-mariadb
fi
ready=0
for attempt in $(seq 1 90); do
    if docker exec general-vm-moodle docker exec moodle-mariadb mariadb-admin ping -uroot -prootpass --silent; then ready=1; break; fi
    sleep 2
done
test "$ready" = 1
docker exec general-vm-moodle bash -c 'chown www-data:www-data /etc/moodle /var/moodledata; chmod 750 /var/moodledata'
docker cp /mnt/lab/scripts/moodle-lab/config.php general-vm-moodle:/etc/moodle/config.php
docker exec general-vm-moodle chown www-data:www-data /etc/moodle/config.php
docker exec general-vm-moodle chmod 640 /etc/moodle/config.php
table_count=$(docker exec general-vm-moodle docker exec moodle-mariadb mariadb -N -umoodleuser -pmoodlepass moodle -e 'SELECT COUNT(*) FROM information_schema.tables WHERE table_schema="moodle"')
if [ "$table_count" = 0 ]; then
    docker exec -u www-data general-vm-moodle php /var/www/html/moodle/admin/cli/install_database.php \
      --lang=en --fullname='No-KVM Moodle laboratory' --shortname='No-KVM Moodle' \
      --adminuser=admin --adminpass='Admin1234!' --adminemail=admin@example.invalid --agree-license
fi
docker exec general-vm-moodle systemctl restart apache2
docker exec general-vm-moodle docker exec moodle-mariadb mariadb -umoodleuser -pmoodlepass moodle -e 'SELECT COUNT(*) AS table_count FROM information_schema.tables WHERE table_schema="moodle"; SELECT username FROM mdl_user WHERE username="admin";'
curl --fail --max-time 30 http://localhost/login/index.php -o /root/moodle-login.html
grep -F 'Log in' /root/moodle-login.html | head -c 400
printf '\nMOODLE_NESTED_STACK_READY\n'

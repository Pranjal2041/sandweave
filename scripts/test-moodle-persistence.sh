#!/bin/bash
set -euxo pipefail
query='SELECT id,shortname,fullname FROM mdl_course WHERE shortname="NOKVM101"'
before=$(docker exec general-vm-moodle docker exec moodle-mariadb mariadb -N -umoodleuser -pmoodlepass moodle -e "$query")
[[ "$before" == *NOKVM101* && "$before" == *'No KVM Integration Course'* ]]
printf 'UI_CREATED_COURSE_BEFORE=%s\n' "$before"
docker exec general-vm-moodle docker inspect moodle-mariadb --format '{{.HostConfig.NetworkMode}} {{json .NetworkSettings.Ports}}'
docker restart -t 30 general-vm-moodle
ready=0
for attempt in $(seq 1 90); do
    after=$(docker exec general-vm-moodle docker exec moodle-mariadb mariadb -N -umoodleuser -pmoodlepass moodle -e "$query" 2>/dev/null || true)
    if [[ "$after" == "$before" ]]; then ready=1; break; fi
    sleep 2
done
test "$ready" = 1
printf 'UI_CREATED_COURSE_AFTER=%s\n' "$after"
docker exec general-vm-moodle systemctl is-active apache2 docker containerd
curl -fsS --max-time 30 http://localhost/login/index.php | grep -F 'Log in to No-KVM Moodle laboratory'
printf 'MOODLE_OUTER_RESTART_PERSISTENCE=PASS\n'

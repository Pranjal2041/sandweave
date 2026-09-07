#!/bin/bash
set -euxo pipefail
docker pull docker:29-dind
docker volume create uml-lab-dind-data
trap 'docker rm -f uml-lab-dind >/dev/null 2>&1 || true' EXIT
SECONDS=0
docker run -d --privileged --name uml-lab-dind -e DOCKER_TLS_CERTDIR= -v uml-lab-dind-data:/var/lib/docker docker:29-dind
ready=0
for attempt in $(seq 1 90); do
    if docker exec uml-lab-dind docker info > /tmp/lab-inner-docker-info.txt 2>&1; then ready=1; break; fi
    sleep 2
done
if [ "$ready" != 1 ]; then docker logs uml-lab-dind; exit 1; fi
printf 'DIND_DAEMON_START_SECONDS=%s\n' "$SECONDS"
cat /tmp/lab-inner-docker-info.txt
docker exec uml-lab-dind docker rm -f inner-web >/dev/null 2>&1 || true
docker exec uml-lab-dind docker run -d --name inner-web -p 8080:80 nginx:1.28-alpine
for attempt in $(seq 1 30); do
    if docker exec uml-lab-dind wget -q -O /tmp/web.html http://127.0.0.1:8080/; then break; fi
    sleep 1
docker exec uml-lab-dind docker logs inner-web --tail 2
done
docker exec uml-lab-dind grep 'Welcome to nginx' /tmp/web.html
docker exec uml-lab-dind docker info --format '{{.Driver}} {{.CgroupVersion}}'
docker exec uml-lab-dind docker exec inner-web sh -c 'id; test ! -e /dev/kvm'
docker image inspect docker:29-dind --format '{{json .RepoDigests}}'
printf 'DIND_TEST_PASS\n'

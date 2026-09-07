#!/bin/bash
set -euo pipefail
test ! -e /dev/kvm
tar --xattrs --xattrs-include='*' --acls --same-owner -xpf /proc/self/fd/3 -C /var/lib
exec 3<&-
echo DOCKER_STORAGE_IMPORTED
if [ "${1:-}" = init ]; then
    exec /sbin/init
fi
mkdir -p /run/docker
rm -f /run/docker.pid
iptables --version
containerd > /tmp/containerd.log 2>&1 &
containerd_pid=$!
for attempt in $(seq 1 60); do
    test ! -S /run/containerd/containerd.sock || break
    sleep .2
done
dockerd --debug --mtu=1500 --containerd=/run/containerd/containerd.sock > /tmp/dockerd.log 2>&1 &
daemon=$!
trap 'kill "$daemon" "$containerd_pid" 2>/dev/null || true; wait "$daemon" "$containerd_pid" 2>/dev/null || true' EXIT
for attempt in $(seq 1 120); do
    if ! kill -0 "$daemon" 2>/dev/null; then
        cat /tmp/dockerd.log
        exit 1
    fi
    if docker info > /tmp/docker-info.txt 2>&1; then
        cat /tmp/docker-info.txt
        docker images
        docker ps -a
        echo DOCKER_DAEMON_READY
        wait "$daemon"
        exit $?
    fi
    sleep .5
done
cat /tmp/dockerd.log
echo 'Docker daemon readiness timed out' >&2
exit 1

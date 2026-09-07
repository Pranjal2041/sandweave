#!/bin/bash
set -euo pipefail
test ! -e /dev/kvm
test "$(id -u)" = 0
printf 'GUEST_KERNEL: '
uname -r
test "$(stat -c %u:%g /home/ga)" = 1000:1000
test "$(stat -c %u:%g /etc/shadow)" = 0:42
test "$(stat -c %a /usr/bin/sudo)" = 4755
mkdir -p /var/tmp/engine-gates
chmod 1777 /var/tmp/engine-gates
printf 'root-only\n' > /var/tmp/engine-gates/secret
chmod 600 /var/tmp/engine-gates/secret
su -s /bin/bash ga -c '
set -euo pipefail
test "$(id -u)" = 1000
if cat /var/tmp/engine-gates/secret 2>/dev/null; then
    echo "FAIL: guest permissions did not deny access" >&2
    exit 1
fi
test "$(sudo -n id -u)" = 0
printf "owned by ga\n" > /var/tmp/engine-gates/ga-file
'
test "$(stat -c %u:%g /var/tmp/engine-gates/ga-file)" = 1000:1000
chown 1234:5678 /var/tmp/engine-gates/ga-file
test "$(stat -c %u:%g /var/tmp/engine-gates/ga-file)" = 1234:5678
echo CREDENTIALS_AND_PERMISSIONS_PASS
mkdir -p /mnt/engine-gates
unshare -m /bin/sh -ec 'mount -t tmpfs none /mnt/engine-gates; echo isolated > /mnt/engine-gates/file; test -f /mnt/engine-gates/file; umount /mnt/engine-gates'
test ! -e /mnt/engine-gates/file
unshare -pf --mount-proc /bin/sh -ec 'test $$ = 1'
echo NAMESPACES_PASS
test "$(stat -f -c %T /sys/fs/cgroup)" = cgroup2fs
echo CGROUP_HIERARCHY_PASS
/usr/local/bin/engine-seccomp-trap
echo SECCOMP_SIGNAL_PASS
for trial in 1 2 3; do
    echo "BENCH_TRIAL=$trial"
    /usr/local/bin/engine-bench
done
echo ENGINE_GATES_PASS

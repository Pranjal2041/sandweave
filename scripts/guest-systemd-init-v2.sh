#!/bin/bash
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t tmpfs tmpfs /run
hostname uml-lab
mkdir -p /mnt/lab
mount -t hostfs -o /lab,ro none /mnt/lab
profile=uml-smp
for argument in $(cat /proc/cmdline); do
    case "$argument" in lab_modules=*) profile=${argument#lab_modules=};; esac
done
[[ "$profile" =~ ^uml-[a-zA-Z0-9-]+$ ]]
release=$(uname -r)
signature="$profile:$(cat "/mnt/lab/tools/$profile/kernel.sha256")"
previous=$(cat /etc/lab-module-signature 2>/dev/null || true)
if [[ "$signature" != "$previous" ]]; then
    test -d "/mnt/lab/tools/$profile/lib/modules/$release"
    mkdir -p /lib/modules
    rm -rf "/lib/modules/$release"
    cp -a "/mnt/lab/tools/$profile/lib/modules/$release" /lib/modules/
    printf '%s\n' "$signature" > /etc/lab-module-signature
fi
exec /sbin/init

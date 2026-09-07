#!/bin/bash
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t tmpfs tmpfs /run
hostname uml-lab
mkdir -p /mnt/lab
mount -t hostfs -o /lab,ro none /mnt/lab
if [ ! -f /etc/lab-smp-modules ]; then
    rm -rf /lib/modules/7.1.3
    cp -a /mnt/lab/tools/uml-smp/lib/modules/7.1.3 /lib/modules/
    touch /etc/lab-smp-modules
fi
exec /sbin/init

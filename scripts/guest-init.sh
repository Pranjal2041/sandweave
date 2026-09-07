#!/bin/bash
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t tmpfs tmpfs /run
hostname uml-lab
mkdir -p /mnt/lab
mount -t hostfs -o /lab,ro none /mnt/lab
mkdir -p /lib/modules
cp -a /mnt/lab/tools/uml/usr/lib/uml/modules/7.1.3 /lib/modules/
ip link set lo up
ip link set vec0 up
ip addr add 10.0.2.15/24 dev vec0
ip route add default via 10.0.2.2
rm -f /etc/resolv.conf
printf 'nameserver 10.0.2.3\n' > /etc/resolv.conf
printf '\nLAB_INIT_READY\n'
ip -br addr
curl -I --max-time 20 https://archive.ubuntu.com/ubuntu/ || true
exec /bin/bash -i

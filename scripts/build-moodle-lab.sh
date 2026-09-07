#!/bin/bash
set -euxo pipefail
mkdir -p /root/moodle-build
cp /mnt/lab/scripts/moodle-lab/* /root/moodle-build/
cp /mnt/lab/downloads/moodle-4.5.13.tgz /root/moodle-build/
docker build -t general-vm-moodle:4.5.13 /root/moodle-build
docker image inspect general-vm-moodle:4.5.13 --format '{{.Id}}'

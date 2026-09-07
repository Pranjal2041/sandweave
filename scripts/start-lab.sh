#!/bin/bash
set -euo pipefail
lab_root=$(cd "$(dirname "$0")/.." && pwd -P)
cd "$lab_root"
export LAB_SSH_PORT=${LAB_SSH_PORT:-22022} LAB_VNC_PORT=${LAB_VNC_PORT:-25901}
[[ "$LAB_SSH_PORT" =~ ^[0-9]+$ && "$LAB_VNC_PORT" =~ ^[0-9]+$ ]]
lock_file=runs/instance.lock
if [[ "$LAB_SSH_PORT" != 22022 ]]; then lock_file="runs/instance-$LAB_SSH_PORT.lock"; fi
exec 9>"$lock_file"
flock -n 9 || { echo 'A lab instance already holds the launch lock.' >&2; exit 1; }
base_image=${LAB_BASE_IMAGE:-images/ubuntu-uml-lab.ext4}
kernel_image=${LAB_KERNEL_IMAGE:-/lab/tools/uml-smp/bin/linux-net}
module_profile=${LAB_MODULE_PROFILE:-uml-smp}
test -f "$base_image"
[[ "$module_profile" =~ ^uml-[a-zA-Z0-9-]+$ ]]
python3 - <<'PY'
import os, socket
sockets = []
for port in (int(os.environ['LAB_SSH_PORT']), int(os.environ['LAB_VNC_PORT'])):
    assert 1024 <= port <= 65535
    sock = socket.socket()
    sock.bind(('127.0.0.1', port))
    sockets.append(sock)
PY
lab_local=$(cat runs/local-path.txt 2>/dev/null || true)
if [[ ! -d "$lab_local" || ! -O "$lab_local" ]]; then
    lab_local=$(mktemp -d "/tmp/general-vm.$(id -u).XXXXXXXX")
    printf '%s\n' "$lab_local" > runs/local-path.txt
    ln -sfn "$lab_local" runs/local
fi
mkdir -p "$lab_local/images" runs/apptainer-tmp downloads/apptainer-cache
base_digest=$(sha256sum "$base_image" | cut -d' ' -f1)
base_cache="$lab_local/images/base-$base_digest.ext4"
if [[ ! -f "$base_cache" ]]; then
    cp --reflink=auto --sparse=always "$base_image" "$base_cache.new.$$"
    mv "$base_cache.new.$$" "$base_cache"
fi
run_name="replay-$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "runs/$run_name/state"
printf '%s\n' "$run_name" > "runs/latest-replay-$LAB_SSH_PORT.txt"
if [[ "$LAB_SSH_PORT" == 22022 ]]; then printf '%s\n' "$run_name" > runs/latest-replay.txt; fi
if [[ ${LAB_DISK_MODE:-cow} == raw ]]; then
    raw_disk="$lab_local/images/$run_name.ext4"
    cp --reflink=auto --sparse=always "$base_cache" "$raw_disk"
    debugfs -w -R 'rm /lab-init' "$raw_disk" > "runs/$run_name/disk-preparation.log" 2>&1
    debugfs -w -R "write $lab_root/scripts/guest-systemd-init-v2.sh /lab-init" "$raw_disk" >> "runs/$run_name/disk-preparation.log" 2>&1
    disk_argument="ubd0=/lab/runs/local/images/$run_name.ext4"
else
    disk_argument="ubd0=/lab/runs/local/images/$run_name.cow,/lab/runs/local/images/base-$base_digest.ext4"
fi
printf '%s\n' "$kernel_image" "$module_profile" "${LAB_CPUS:-4}" "${LAB_SECCOMP:-on}" "$disk_argument" > "runs/$run_name/launch-settings.txt"
printf 'Run: %s\nLog: %s/runs/%s/console.log\nSSH port: %s\nVNC: 127.0.0.1:%s (password labvnc01)\n' "$run_name" "$lab_root" "$run_name" "$LAB_SSH_PORT" "$LAB_VNC_PORT"
exec scripts/run-uml.sh "$kernel_image" \
    "mem=${LAB_MEMORY:-4096M}" "ncpus=${LAB_CPUS:-4}" "seccomp=${LAB_SECCOMP:-on}" \
    "umid=$run_name" "uml_dir=/lab/runs/$run_name/state" \
    "$disk_argument" "lab_modules=$module_profile" \
    root=/dev/ubda rw con=null con0=fd:0,fd:1 \
    'vec0:transport=vde,vnl=slirp://' init=/lab-init \
    systemd.unified_cgroup_hierarchy=1 > "runs/$run_name/console.log" 2>&1

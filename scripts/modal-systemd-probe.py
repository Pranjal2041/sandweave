#!/usr/bin/env python3
"""Probe whether systemd can boot as a Modal sandbox's init, with or without a GPU.

Records PID 1, capabilities, cgroup filesystem and systemd state for each entrypoint.
Requires Modal credentials (MODAL_ENVIRONMENT selects the environment).
"""
import argparse
import json
from pathlib import Path
import time

import modal

APP = 'sandweave-modal-runtime-lab'
SHIM = Path(__file__).with_name('modal-guest-init.py')
GUEST = r'''
P=$(cat /tmp/sandweave-init.pid)
nsenter -t "$P" -U -m -p sh -c '
echo "guest_pid1=$(cat /proc/1/comm)"
timeout 60 systemctl is-system-running --wait; echo "systemctl_exit=$?"
systemctl --failed --no-pager | head -30
cat /proc/1/cgroup
printf "[Service]\nType=oneshot\nExecStart=/bin/sh -c \"cat /proc/self/cgroup > /tmp/probe.cgroup\"\n" > /etc/systemd/system/probe.service
systemctl daemon-reload; systemctl start probe.service; echo "probe_start_exit=$?"
cat /tmp/probe.cgroup 2>&1
'
grep -a -i -E "cgroup|attach|219|Failed" /tmp/sandweave-init.log | grep -v -i "unit configured to use KillMode" | head -40
'''
PROBE = r'''
echo "pid1=$(tr '\0' ' ' < /proc/1/cmdline)"
echo "self=$$ uname=$(uname -r)"
grep -E '^Cap(Prm|Eff|Bnd)' /proc/self/status
stat -fc 'cgroupfs=%T' /sys/fs/cgroup 2>&1
ls /sys/fs/cgroup 2>&1 | head -20 | tr '\n' ' '; echo
cat /proc/self/cgroup
mount | grep -E 'cgroup|/run |/tmp ' | head
unshare --user --map-root-user --pid --fork --mount-proc true && echo userns_pidns=ok || echo userns_pidns=failed
timeout 60 systemctl is-system-running --wait 2>&1; echo "systemctl_exit=$?"
systemctl --failed --no-pager 2>&1 | head -30
journalctl -b --no-pager -n 5 2>&1 | tail -5
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gpu', default=None)
    parser.add_argument('--entrypoint', choices=('init', 'sleep', 'shim', 'shim-cgroup'), required=True)
    args = parser.parse_args()
    app = modal.App.lookup(APP, create_if_missing=True)
    image = (modal.Image.from_registry('ubuntu:22.04')
             .apt_install('systemd', 'systemd-sysv', 'dbus', 'procps', 'util-linux', 'python3')
             .add_local_file(SHIM, '/opt/sandweave/modal-guest-init.py', copy=True))
    command = {'init': ['/sbin/init'], 'sleep': ['sleep', 'infinity'],
               'shim': ['sh', '-c', 'exec python3 /opt/sandweave/modal-guest-init.py --pid-file '
                        '/tmp/sandweave-init.pid -- /sbin/init --log-target=console --log-level=debug '
                        '> /tmp/sandweave-init.log 2>&1'],
               # Whether the guest init and its children are members of the
               # systemd hierarchy, and whether either can migrate itself.
               'shim-cgroup': ['python3', '/opt/sandweave/modal-guest-init.py', '--', '/bin/sh', '-c',
                               'grep systemd /proc/self/cgroup || echo pid1_not_member; '
                               'mkdir /sys/fs/cgroup/systemd/a; echo 0 > /sys/fs/cgroup/systemd/a/cgroup.procs '
                               '&& echo pid1_write_ok; grep systemd /proc/self/cgroup; '
                               'sh -c "grep systemd /proc/self/cgroup || echo child_not_member; '
                               'mkdir /sys/fs/cgroup/systemd/b; echo 0 > /sys/fs/cgroup/systemd/b/cgroup.procs '
                               '&& echo child_write_ok; grep systemd /proc/self/cgroup"; sleep 8']}[args.entrypoint]
    probe = GUEST if args.entrypoint == 'shim' else PROBE
    sandbox = modal.Sandbox.create(*command, app=app, image=image, gpu=args.gpu, cpu=2, memory=4096,
                                   timeout=900)
    result = {'sandbox': sandbox.object_id, 'gpu': args.gpu, 'entrypoint': command}
    try:
        if args.entrypoint == 'shim-cgroup':
            sandbox.wait(raise_on_termination=False)
        else:
            time.sleep(5)
        if sandbox.poll() is None:
            process = sandbox.exec('bash', '-c', probe, timeout=180)
            output = process.stdout.read() + process.stderr.read()
            result.update(exit=process.wait(), output=output)
        result['poll'] = sandbox.poll()
    finally:
        sandbox.terminate()
    if result['poll'] is not None:
        # The entrypoint exited: its own output explains why.
        result.update(entrypoint_returncode=sandbox.returncode, entrypoint_stdout=sandbox.stdout.read()[-8000:],
                      entrypoint_stderr=sandbox.stderr.read()[-8000:])
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()

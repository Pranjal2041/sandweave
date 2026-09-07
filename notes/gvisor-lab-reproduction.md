# Reproducing the no-KVM Linux prototype

Independent lab: `/data/user_data/pranjala/general-vm` (`~/scratch/general-vm`).
The Gym Anything project is not modified or executed. UML remains paused.

This uses the existing ready Ubuntu filesystem, unchanged Firefox/Earth/Moodle
binaries and actual Docker image/volume data. Sentry provides the Linux system
interfaces; systrap executes application instructions natively. Guest root,
guest UIDs and nested namespaces live inside Sentry. The host processes remain
the cluster user's UID. Apptainer hides `/dev/kvm` and the launcher asserts its
absence before selecting systrap. Internet and inbound TCP traverse an
unprivileged Ethernet socket -> relay -> passt. No host sudo, KVM, TAP setup,
host firewall edits, subordinate UID ranges, or admin changes are used.

## Build and start

The separate gVisor checkout is on `experiment/no-kvm-slurm`, based on
`0a1316b0d180600212bd607aa0ccfe2a9b09a899`. The build runs in the pinned upstream
Apptainer builder; its digest is in `runs/gvisor-builder-ref.txt`.
The prototype changes are committed as
`a169d9a5075b258568a51232b976a765e0a83b29`, followed by EROFS xattr/ACL support in
`ae303ca510bb171dd5e498783e2e18a56d67ad05`. Resource controls and full networking snapshots are committed through `8c8b1437b27ce0fb61a9db17c5feb33242b7bde3`; a cumulative diff is available in
`notes/gvisor-no-kvm-prototype.patch`. The subsequent signed futex correction in
`1bfaec6` enables Resolve's Qt semaphore; see [Resolve acceptance](resolve-gpu.md).
GPU device donation is corrected in `b832209`; it restores completion wakeups
and 24-fps Resolve playback. See [GPU notification repair](resolve-gpu-notifications.md)
for current runtime, reproducer and desktop ports.

```bash
cd ~/scratch/general-vm
python scripts/stage-gvisor.py
python scripts/run-gvisor.py --detach --nftables --guest-gs --docker-data --cgroup v1 \
  --docker-archive images/gvisor-moodle-persisted-docker.tar \
  demo -- /usr/local/bin/engine-docker init
```

`--detach` keeps the launcher independent of the chat/terminal process group.
It prints the launcher PID and log path; that message does not mean boot is
finished. Check the guest service or actual VNC connection for readiness.
Use a new sandbox name for each run; existing bundles are now rejected before
any modification. Logs and the allocated host loopback ports
are written under `runs/gvisor/demo/`; VNC is the `5901` entry of `ports.json`.
The lab VNC password is `labvnc01`. `--cpus` selects a shared eligible CPU pool, defaulting to the inherited Slurm allocation. New launches default to weight 100, four advertised guest CPUs, an 8 GiB guest-page budget and a separate 1 GiB runtime guard. `--ignore-cgroups` avoids unavailable host cgroup delegation; enforcement comes from the custom allocator/controller, with the limits described in `resource-snapshot-implementation.md`. Staging builds all runtime binaries and publishes an immutable version.

The data bootstrap restores a read-only archive into guest filesystems, then
executes Ubuntu systemd. Once Docker is ready, explicitly start the saved
Moodle container; once its inner Docker is ready, start MariaDB. A clean data
export marks the saved container manually stopped, so `unless-stopped` will
not auto-start it. Both retain `CgroupnsMode=private`, normal bridges, published
ports and real overlay filesystem storage. Moodle's canonical URL is
`http://localhost` from the browser INSIDE the guest. For host HTTP probes, use
the allocated port with `Host: localhost` to avoid following a redirect to the
host's unrelated port80.

Commands in an existing sandbox:

```bash
scripts/gvisor-host.sh /lab/tools/gvisor-socket/runsc \
  --root=/local/gvisor/state exec demo systemctl is-active docker
scripts/gvisor-host.sh /lab/tools/gvisor-socket/runsc \
  --root=/local/gvisor/state exec demo docker start general-vm-moodle
# Run after the inner Docker daemon is ready:
scripts/gvisor-host.sh /lab/tools/gvisor-socket/runsc \
  --root=/local/gvisor/state exec demo \
  docker exec general-vm-moodle docker start moodle-mariadb
scripts/gvisor-host.sh /lab/tools/gvisor-socket/runsc \
  --root=/local/gvisor/state exec demo \
  docker exec general-vm-moodle docker ps
```

For a full systemd guest, stop the laboratory sandbox with `kill --all demo
KILL`; PID1 can ignore TERM and restart services. Keep original experiment
images and data archives; do not delete the source ext4.

## Durable container data

`scripts/export-gvisor-docker.py` stops Moodle and its parent Docker/containerd
cleanly, exports numeric ownership, modes, xattrs and ACL records into a tar
archive, then starts the daemons and Moodle again. It refuses to overwrite an
archive. A SHA256 manifest accompanies the output.

```bash
python scripts/export-gvisor-docker.py demo images/my-docker-state.tar
python scripts/run-gvisor.py --detach --nftables --guest-gs --docker-data --cgroup v1 \
  --docker-archive images/my-docker-state.tar \
  resumed -- /usr/local/bin/engine-docker init
```

This export preserves container state, including Moodle data/config, its inner
Docker state and MariaDB volumes. It does not export arbitrary edits elsewhere
in the guest root filesystem or the guest browser profile.

## Engine changes and limitations

Implemented an unprivileged Ethernet FD backend; nftables batch compatibility
and real TCP/UDP xtables matches; application GS preservation with binary syscall
patching disabled; co-mounted v1 CPU/cpuacct support; named cgroup inheritance;
v1 cgroup namespace paths, mounts, clone/unshare/setns; name-only hierarchy
lookup and pids.max parsing. Live probes and relevant upstream unit tests are
recorded in `gvisor-prototype-progress.md` and `runs/gvisor`.

Whole running Linux environment checkpoints are now validated, including Moodle, nested Docker/MariaDB, Firefox, RAM, open deleted files, queued Unix sockets and established TCP across namespaces. The runtime now saves bridge/network topology, addresses/routes, nftables/iptables and conntrack. A second checkpoint of a restored desktop also restored and accepted VNC navigation. Use `python scripts/checkpoint-gvisor.py ENV LABEL`, then `python scripts/run-gvisor.py --detach --restore snapshots/LABEL NEW_ENV`. External peers do not roll back with the snapshot. See `resource-snapshot-implementation.md` for exact evidence and dependencies.

EROFS now reads inline/shared xattrs, handles inline file tails after xattrs,
and enforces access ACLs. The original journal directory is accessible, its
ACLs survive overlay copy-up, and new directories inherit its default ACL.
Native comparison and named-user tests: `python scripts/test-erofs-xattrs.py`.
The supported image formats are unchanged: incompatible features such as long
xattr prefixes remain rejected, and the full Ubuntu image still uses
`noinline_data` for mmap compatibility. This does not add SELinux enforcement.
Device policy, arbitrary kernel-dependent workloads,
3D graphics performance and broader reliability need separate qualification. This is an independently runnable
prototype demonstrating a substantive subset, not a complete replacement for
all Linux VM behavior.


## Current interactive sessions (2026-09-07 02:12 UTC)

Node `babel-u5-28`; both launchers are detached session leaders with parent PID1.
The earlier ports41777/42837 no longer apply. Password for both: `labvnc01`.

| Sandbox | Application | Host loopback VNC port | Suggested local forwarded port |
|---|---|---:|---:|
| moodle-user1 | Firefox155 and full nested Moodle/MariaDB | 40377 | 5907 |
| earth-user1 | Google Earth Pro7.3.7 | 40047 | 5908 |

From your workstation, using your usual jump host if required:

```bash
ssh -N -L 5907:127.0.0.1:40377 -L 5908:127.0.0.1:40047 pranjala@babel-u5-28
```

In TigerVNC, use `127.0.0.1::5907` or `127.0.0.1::5908`. Keep the viewer's
compression automatic, or select ZRLE. Moodle is already logged in as admin;
its lab credentials are `admin` / `Admin1234!`. The course URL inside Firefox is
`http://localhost/course/view.php?id=3`.

Fresh Firefox profiles need a directory owned by ga before invoking
`/opt/firefox/firefox --no-remote --profile /home/ga/engine-firefox`.
For both applications, wait until GNOME has established its window manager
(check `_NET_SUPPORTING_WM_CHECK` with xprop on DISPLAY=:1) before launching.
Starting Earth earlier produced an incompletely drawn window; restarting it
after GNOME was ready corrected that.

Earth uses software Mesa llvmpipe with `LP_NUM_THREADS=4`, four host CPUs8-11,
and 1280x800 VNC. Two six-second compressed-VNC drags delivered8.24/8.07 changed
updates/s, first changes306/258ms and worst gaps188/195ms. Raw VNC delivered2.52
updates/s. These are end-to-end delivered updates, not the application's FPS.
The older native controls delivered about12 updates/s but used a different
window manager and network path, so they are not an isolated engine comparison.

The benchmark's ZRLE decoder was repaired in `scripts/vnc_zrle.py` to respect
byte padding at the end of every palette row (RFC6143). This affects only our
measurement client; ordinary VNC viewers use their own decoders. Tests:
`tools/visual-bench-venv/bin/python scripts/test-vnc-zrle.py`.

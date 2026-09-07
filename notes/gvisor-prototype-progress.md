# No-KVM prototype: implementation results, 2026-09-06

User authorized implementation after the architecture investigation. Project
code remains unchanged and unexecuted. UML remains paused.

Node: babel-u5-28. Independent lab: `/data/user_data/pranjala/general-vm`.
Local storage path: `runs/local-path.txt`. Every runtime launch uses Apptainer
to hide `/dev/kvm`, asserts its absence, and explicitly selects systrap.
Host UID/GID stays 2710891; no host sudo or subordinate UID ranges are used.

## Passed upstream baseline

Nightly 2026-09-06, binary reports release-20260831.0-12-g602040cbcc58-dirty.
Full ready Ubuntu image converted to EROFS with original UIDs/GIDs/modes/xattrs.
The original ext4 is mounted read-only. `scripts/build-gvisor-erofs.sh` uses
`noinline_data`, required by upstream EROFS mmap support. A converter-only
preload helper reads inaccessible trusted xattrs from the immutable ext4 by
inode using debugfs; attributes are preserved, not dropped.

`runs/gvisor/gates3.out` records successful guest root and UID1000 transitions,
permission denial, sudo, arbitrary chown, mount/PID namespaces, cgroup2fs, and
seccomp SIGSYS context checks. This is cgroup hierarchy support, not proof of
nested resource limit enforcement.

Three trials, pinned to host CPUs 0-3, compared identical benchmark binaries:
native `runs/gvisor/native-gates-bench.out`; guest `runs/gvisor/gates3.out`.
Median single worker: native .317791 s, guest .317384 s.
Median four threads (four times the work): native .322363 s, guest .337307 s.
Median four processes: native .324988 s, guest .337078 s.
Median 20,000 raw getpid calls: native .004936 s, guest .070088 s (14.20x).
These establish concurrent native instruction execution and syscall overhead;
they do not establish application responsiveness.

## Network implementation in progress

Separate `sources/gvisor` checkout, branch `experiment/no-kvm-slurm`.
Build runs under an unprivileged Apptainer image pinned to the upstream builder
digest in `runs/gvisor-builder-ref.txt`; node-local Bazel cache and /tmp.
`scripts/build-gvisor.sh` builds without host compiler installation.

New `--network-socket-config` supplies one IPv4 Ethernet interface through an
unprivileged SOCK_SEQPACKET FD while retaining Sentry netstack. No host network
interface setup occurs in that path. Segmentation/checksum offloads disabled
on the link so the external transport receives complete Ethernet frames.
`scripts/ethernet-relay.py` adapts those messages to passt's four-byte big-endian
length-prefixed Unix stream protocol, verified against upstream tap.h.

Engine config/sandbox tests pass, source runsc and matching Sentry built.
Relay tests pass: bidirectional frames, fragmented/coalesced stream input,
invalid lengths, truncated payload and disconnect cleanup.
## Further implementation and live results (through 2026-09-07 00:48 UTC)

DNS, outbound HTTPS, and inbound forwarded HTTP pass in `runs/gvisor/net4`.
The source engine retains normal guest netstack, namespaces, and Docker bridges.

Two nftables batch defects are fixed: accepting legacy host-order subsystem IDs
and responding with each message's sequence number. Ubuntu's iptables-nft and
Docker now successfully install rules. Raw-message probes pass on native Linux
and the patched guest (`nft-native.out`, `nft2`).

A third upstream defect made TCP/UDP xtables compatibility matches unconditional
successes. Docker's first published-port DNAT rule consequently captured every
TCP port, including VNC/SSH. Implemented revision-zero TCP/UDP port ranges,
inversions, TCP flags/options and malformed-packet handling. nftables unit tests
pass. Differential probe: upstream guest sends ports 8000 and 8080 to SERVICE_A;
patched guest sends them to SERVICE_A and SERVICE_B respectively, and leaves
unrelated ports alone (`nat2/port-isolation.out`). Native Linux matches the
patched dispatch behavior (`nat-native.out`).

### Original container filesystem and real Docker

The EROFS image preserves original metadata on disk, but upstream gVisor cannot
read inodes with xattrs; its EROFS Get/ListXattr are unsupported. Therefore the
earlier image-conversion result does NOT establish usable xattr preservation.
In particular Docker's overlay metadata and /var/log/journal are affected.

Exported the original /var/lib/docker and /var/lib/containerd, with numeric
owners, ACLs and xattrs, into a 3,349,872,640-byte read-only archive. The launcher
passes this ordinary file FD into the guest, where GNU tar restores it into
dedicated launcher-provided tmpfs mounts. This preserves the actual container
images/state; no replacement applications or unprivileged ownership rewriting.
EROFS root xattrs and persistent writable storage are still unfinished.

The root Docker daemon starts, its real Moodle outer container boots its own
systemd, Apache, containerd and Docker. A separate nginx container starts, uses
normal bridges, and reaches outbound HTTPS. Published-port isolation now passes
after the xtables fix. Inner MariaDB remains a gate, not a passed result.

### Desktop and unchanged Firefox: GS fix

Full Ubuntu systemd + GNOME + TigerVNC boot in cgroup v2 (`desktop3`). Firefox
155 and ESR140 originally both aborted at ARCH_SET_GS. Upstream rejects that
ABI; systrap's binary syscall patches reserve GS for its internal thread state.
Changing Firefox versions did not address it.

Implemented application GS save/restore in systrap's signal handler and
ARCH_GET_GS/ARCH_SET_GS, enabled only when binary syscall patching is disabled.
The Sentry/guest isolation boundary is retained. Guest application threads still
execute native instructions concurrently. No Firefox rebuild or sandbox-disable
flags. A native and guest C differential probe verifies four threads, repeated
GS memory accesses, signals, fork inheritance, EFAULT/EPERM and state after error
(`gs-base-native.out`, `gs1/guest.out`). Existing systrap tests pass too.

Unchanged Firefox 155 runs headfully, loads HTTPS example.com, and accepts VNC
interaction. Viewed screenshots: `desktop3/firefox-page.png` and
`desktop3/firefox-support.png`. Guest content processes use seccomp. Diagnostic
data opt-in was deselected in first-run UI; no crash reports were submitted.

An isolated three-trial run on CPUs 8-11 with GS compatibility enabled records
20k getpid median .067939 s vs native .004921 s (~13.8x); CPU-single .318238 vs
.317846 s; four threads .336634 vs .335061 s; four processes .340714 vs .318482 s.
These are microbenchmarks, not a responsiveness guarantee. The existing getpid
benchmark may not contain the instruction pattern eligible for binary patching,
so this does not quantify that optimization's full performance tradeoff.
The earlier desktop3 benchmark contended with active GUIs on CPUs0-3; use the
isolated `gsbench`/`gsbench-native.out` pair instead.

Google Earth Pro 7.3.7 renders and responds to mouse drags under systrap, using
software graphics. Viewed actual before/during frames in
`desktop2/earth-drag-raw`. This trial delivered ~2.35 changed VNC updates/s,
234ms first visible change, with a 1.54s worst gap. Both GUI guests were sharing
four CPUs during this trial, so it is not a clean engine-performance comparison.
The first ZRLE measurement failed in vncdotool's decoder; the raw-encoding retry
succeeded. Smooth 3D performance is not yet demonstrated.

### Nested Docker/cgroups, currently being resolved

Cgroup v2 inner runc fails retrieving an attached BPF program by ID. Source
inspection found every upstream program assigned ID0, missing GET_FD_BY_ID, and
explicitly unimplemented BPF validation/execution. Do not fake successful calls
or claim device-policy enforcement.

For the Ubuntu249/systemd environment, testing the supported legacy v1 layout.
Fixed co-mounted CPU+cpuacct setup and lookup by one controller in a shared
hierarchy. A follow-up exposed a separate bug: computeInitialGroups dropped
named hierarchies without controllers (systemd's hierarchy) during fork. Fixed
inheritance and initial root placement for named hierarchies. Existing kernel
tests pass; fresh full boot `desktop5` is the next live check.

Upstream v1 device rules also appear to be stored without access enforcement.
Full cgroup resource/device semantics are not established by a successful
container launch. Root EROFS xattrs, persistence, general workload compatibility,
and clean application performance remain required work. Overall goal is NOT
complete.

## 01:00 UTC: nested database and state restoration pass

Implemented v1 private cgroup namespaces: capture a referenced root per
hierarchy at clone/unshare, preserve it across migrations, virtualize /proc
membership and mountinfo paths, root fresh mounts at that namespace root,
support namespace descriptors and setns. Added a live test checking named
hierarchy inheritance, ancestor /.. paths, sibling invisibility on a fresh
mount, root stability after migration, and setns restoration. Pass recorded in
`cgns3/guest.out`. Semantics compared with Linux man-pages cgroup_namespaces(7).
Further corrected name-only cgroup remount lookup and parsing pids.max='max'.

`desktop7` now boots the ORIGINAL cached Moodle outer container with its
existing privileged/private-cgroup-namespace configuration, original volumes,
original bridge and -p80:80. Its own Docker successfully starts the ORIGINAL
moodle-mariadb container, also with a private cgroup namespace, bridge network,
and 127.0.0.1:3306 publication. SQL finds the original installed Moodle database
and previously created NOKVM101 course. Guest HTTP follows Moodle's localhost
redirect and returns 200 and the real home page. `moodle-start.out`,
`moodle-db.out`, `moodle-functional.out` record this. The first root SQL attempt
used the wrong environment variable for the password and failed; retry using
the known lab Moodle DB account passed. No auth policy was changed.

First host HTTP probe followed the application's canonical redirect to host
localhost:80 and failed there; this was NOT a guest port-forward failure.
The browser inside the guest uses the configured canonical http://localhost.

The current source checkpointed `persist1`, then restored into fresh `persist3`
with original OCI spec retained. File contents, 1234:5678 ownership, mode640,
user.engine xattr and outbound HTTPS pass after restore
(`persist3/restored-state.out`). The first restore's new fixture annotation
path failed strict spec validation; preserving the original spec corrected
the launcher. Full Moodle/GUI checkpoint restore is still pending.

Firefox in desktop7 is launched as a systemd unit so it can survive a full
checkpoint; runsc-exec children are explicitly not restorable by upstream.
Screenshot `desktop7/moodle-onboarding.png` shows the unchanged Firefox155
rendering the real Moodle home page with the preserved course, behind the
first-run dialog. Continuing UI and persistence validation now.

## 01:10 UTC: completed Moodle GUI and durable data round trip

In desktop7, logged into unchanged Firefox155/Moodle through VNC and created
`SYSTRAP101` / `Systrap No KVM Integration Course` using the actual course form.
Viewed `desktop7/moodle-created-course.png`; SQL in the inner MariaDB confirms
course ID3. Both root and inner Docker use overlayfs. Both containers retain
private cgroup namespaces; `/proc/self/cgroup` inside MariaDB shows `/` for each
hierarchy. Guest sudo works inside Moodle. `/dev/kvm` is absent at all three
layers. Five host-forwarded requests to Moodle's real login page with canonical
Host header take 137, 47, 24, 22 and 21ms (median24ms). This measures server HTTP
responses, not total browser rendering latency.

Restarted the entire outer Moodle container: the course and HTTP200 survive.
Then cleanly stopped Moodle and the parent Docker/containerd daemons, exported
their filesystem state to `images/gvisor-moodle-persisted-docker.tar`, and
restarted the source stack. Export: 3,350,026,240 bytes, SHA256
`18b7fb3c9610706b9f67be4924467c3adf4699923018b0123fcec51bed2666b7`.
Started a FRESH sandbox `desktop8` from that archive and the immutable Ubuntu
base. Explicitly started the saved containers (clean export marks the outer
container manually stopped; unless-stopped does not auto-start it). The inner
MariaDB recovers the same UI-created course and Moodle returns200:
`desktop8/cold-restored-course.out`. This is durable container-data persistence
across sandbox creation, not merely restarting a process in the same memory.
It does not persist arbitrary root/home edits outside the exported paths.

Full warm checkpoint remains unfinished. The complete stack failed serialization
of `stack.BridgeFDBEntry`; added its missing stateify annotation and networking
unit tests pass. More fundamentally, `Stack.nftables` is marked `state:nosave` in
upstream, so merely repairing that serializer would not preserve Docker rules.
Do not claim complete warm restore. Filesystem-only checkpoint also rejects
the current shared-memory-backed mounts as non-checkpointable; cold Docker
archive import/export works independently of this limitation.

Final focused upstream tests: kernel, systrap, nftables, runsc config, sandbox,
and networking stack all pass; relay's3 tests pass; native/guest GS probe,
native/guest TCP DNAT dispatch and guest cgroup namespace/inheritance probes
pass. Native unprivileged cgroup-namespace creation/path probe is additionally
recorded; native v1 controller mount tests cannot safely be performed against
the cluster's live hierarchy. Namespace semantics reference:
https://man7.org/linux/man-pages/man7/cgroup_namespaces.7.html

Reproduction guide: `notes/gvisor-lab-reproduction.md`. The supported result of
this round is a working, independently reproducible Linux workload subset. The
general replacement goal remains incomplete, with limits explicitly described
above. No Gym Anything project code was changed or run; UML was not resumed.

Prototype source commit: `a169d9a5075b258568a51232b976a765e0a83b29`, 24 files,
582 insertions/79 deletions. Standalone patch:
`notes/gvisor-no-kvm-prototype.patch`. The separate source checkout is clean.
The temporary desktop8 cold-restore guest is stopped after verification;
desktop7 remains available for inspection through VNC (host loopback41777).
The saved Docker archive is on persistent lab storage, not node-local /tmp.


## 2026-09-07 02:12 UTC: interactive handoff and root metadata support

Implemented inline/shared EROFS xattr decoding, bounds checks, inline data
positioning after the xattr body, path/fd Get/ListXattr, trusted namespace
visibility, capability-value namespace fixup, ACL loading and access checks,
and GetPosixACLAt for overlay copying/inheritance. Incompatible EROFS features
remain rejected. Commit ae303ca510bb171dd5e498783e2e18a56d67ad05.

Four focused suites pass (EROFS, VFS, kernel, sandbox), with runsc and Sentry
rebuilt. Real mkfs.erofs fixtures match native Linux for file contents,
path/fd xattr values, binary/empty/shared values, ACL bytes, missing attrs,
ERANGE and size queries. A named UID1000 guest probe verifies grant versus ACL
mask denial for reads/user xattrs and readable ACL metadata. The first named
probe failed because the source fixture directory inherited mode750 from the
cluster umask; explicit fixture mode755 fixes the test, without an engine change.
The reproducible test driver is scripts/test-erofs-xattrs.py; output in
runs/gvisor-xattr-live-tests.log and runs/gvisor/xattrs-1788747066959499286.

The ORIGINAL /var/log/journal is now accessible with its original ACLs. ACLs
survive an overlay copy-up and are inherited by a new directory. Checkpoint
xattr-state1 restores those attributes and the new child into xattr-restored1
successfully (restored-attrs.out). This remains a small non-nftables checkpoint;
it does not establish full nested Docker/GUI warm restore. Both old iptables
and current nftables state are marked nosave upstream; conntrack/timer state
also needs correct restoration. No rules were discarded to claim success.

Google Earth was tested before this round; this round additionally exercised
real search for Golden Gate Bridge, loaded satellite imagery and map drags.
Early earth1 launched before GNOME was ready, producing partially drawn UI;
restarting after the WM initialized fixed the drawing. A user continuation
interrupted the foreground launchers, stopping desktop7 and earth1. Replaced
with detached moodle-user1/earth-user1, both verified as session leaders with
parent PID1. Moodle was restored from the durable archive; the saved course is
present. Firefox's new profile was created, onboarding completed with diagnostic
sharing disabled, logged into Moodle, and opened course3. A first rapid URL
entry lost leading characters during the focus transition; retry with a short
focus-settle pause navigated correctly. No claim of perfect input latency.

Added run-gvisor.py --detach and early fresh-name validation. Its bounded launch
probe detach-test1 survives command exit, prints DETACHED_LAUNCH_PASS and exits0.
The Moodle/Firefox session is VNC40377, Earth VNC40047 on host loopback; password
labvnc01. Original ports41777/42837 are no longer live. No project code changed;
UML remains paused.

Earth LP_NUM_THREADS=4 on CPUs8-11, software llvmpipe, 1280x800, six-second drags:
- Raw VNC: 2.5206 changed viewport updates/s; first840ms; largest gap1073ms.
- ZRLE: 8.2351 and8.0655 updates/s; first306/258ms; largest gaps188/195ms.
Evidence: earth-user1/drag-lp4, drag-zrle, drag-zrle2. Viewed captured frames.
The compressed client compares its initial viewport with a verified raw image
in the first run. Fixed the lab client's packed-palette row-padding bug and
used vectorized decode; three decoder tests pass. RFC6143 sections7.7.5-6:
https://www.rfc-editor.org/rfc/rfc6143.html
This is a client/transport improvement, not an Earth binary change or GPU use.
The native controls' approximately12 updates/s used different WM/network setup;
do not attribute the remaining gap to one engine component without profiling.
General syscall overhead, full warm restore, cgroup policy enforcement and
broader Linux compatibility remain outstanding.


## Resource and isolation status check: 2026-09-07T02:22:27.458994+00:00

Both interactive sandboxes remain running. Moodle root Docker, inner MariaDB,
Firefox and VNC are active; Moodle HTTP200. Earth and VNC are active.
Guest CPU affinity: Moodle0-3, Earth8-11, four logical CPUs each. These are
not exclusive reservations. Each entire guest (including GUI and all Docker
layers) shares its four CPUs. Host passt/relay helpers are not pinned to the
same four CPUs and inherit the broader Slurm step CPU set0-13,44-57.

Both OCI specs declare8GiB but --ignore-cgroups leaves that per-environment
limit unenforced. The inherited Slurm job/user ceiling is320GiB shared with
other processes in that allocation. Guest /proc/meminfo reports the node's
approximately755GiB total, not an actual guest allocation. Current engine
application accounting is approximately4.65GiB for Moodle/Firefox and0.74GiB
for Earth; Moodle includes approximately3.31GiB tmpfs container storage.
These figures omit some runtime/helper memory and immutable-image page cache.
Do not sum host process RSS/PSS blindly: systrap sysmsg tasks use CLONE_VM
without CLONE_THREAD, so per-PID aggregation can count the same mm repeatedly.

Fresh non-destructive namespace checks: both guests lack /lab, /local and
/data/user_data/pranjala/general-vm; each /proc hides the other Sentry's host
PID and kill(pid,0) returnsESRCH. Each guest has its own systemd PID1.
Cross-env networking policy is NOT enforced: guest localhost refuses the
other guest's host-forwarded VNC port, but gateway10.0.2.2 reaches it from
both directions. These are private network stacks with working networking,
not mutually blocked networks. Host-service and cross-environment access
policy needs explicit enforcement. No VNC authentication was attempted by
these TCP-connect probes, and user desktop configuration was not modified.

File/UID/ACL/mount and PID namespace behavior has positive and negative
functional tests; the custom engine has not had a complete adversarial
sandbox review. Resource/device controller enforcement remains incomplete.
Machine-readable evidence: runs/gvisor/resource-isolation-status.json.


## 2026-09-07: resource controls and whole-desktop snapshots accepted

The four requested workstreams are implemented and live tested in the independent lab. Weighted CPU sharing is the default, with optional average quotas and idle borrowing. Guest page budgets, runtime heap guards, and external network policy have positive/negative tests. See `resource-snapshot-implementation.md` for measurements and boundaries.

Full snapshot iterations exposed missing save/restore state in network namespaces, IP addresses, veth queues, neighbor timers, TCP segmentation settings and conntrack handler functions. Fixes retain internal networking and established TCP rather than dropping them. Trial 11 accidentally staged an old Sentry after building only the separate CLI; staging now builds every runtime component. Trial 12 passed generic state, full Moodle/nested Docker, Firefox, internet and isolation checks. A second snapshot of the restored, logged-in desktop also passed and its actual VNC participants/course navigation was inspected.

Accepted source commit: `8c8b1437b27ce0fb61a9db17c5feb33242b7bde3`. Recommended running snapshot: `snapshots/full-desktop-ready`. Implementation recovery: `checkpoints/resources-snapshots-8c8b143-r3`. New interactive restored desktop: `ready-clone2`, VNC47217. Original user desktops on40377/40047 are preserved and retain their previous configuration. Failed diagnostics remain labeled and available.

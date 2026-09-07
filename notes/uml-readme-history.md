# General VM: independent no-KVM experiments

This directory is a standalone lab created on 2026-09-06 in the existing Slurm
allocation on `babel-u5-28`. It uses no gym-anything code, images, or runtime.
No host sudo, host package installation, or administrator changes were used.

The current single-CPU UML configuration has exercised headful Firefox155 with
its sandbox enabled, Google Earth Pro with real imagery and a saved placemark,
and Moodle4.5 with a UI-created course that survives restarting the outer Docker
container. Moodle uses actual nested Docker for MariaDB. The four-CPU engine
remains under investigation: some Firefox sessions work, while earlier fresh
profiles hung or aborted. ESR140 has passed a fresh-profile input test on four
CPUs. These are application compatibility results, not proof of full-task speed
or validation of every project environment.

Current evidence: `notes/ptrace-application-progress.md` and
`notes/browser-version-comparison.md`. The initial-round results below are
retained as history; they are superseded where those notes report later tests.
The clean initialized application checkpoint is `images/ubuntu-uml-ready.ext4`,
SHA256 in `notes/ready-image-sha256.txt`. Its filesystem check passed after a
normal guest shutdown. Per-run raw disks from abrupt termination are not clean
checkpoints.

## Architecture actually exercised

```text
Slurm task, real UID 2710891
  Apptainer --userns --containall (Debian glibc; /dev/kvm absent)
    Linux 7.1.3 built ARCH=um, 4 guest CPUs, 4 GiB RAM
      Ubuntu 22.04.5 filesystem, systemd PID 1, cgroup v2
        Docker -> Compose MariaDB + nginx
               -> privileged docker:dind -> inner Docker -> nginx
        GNOME 42.9 + TigerVNC + Firefox 155.0.1
```

UML runs a Linux kernel as an ordinary host process. This experiment replaces
the guest kernel and boot plumbing while retaining normal Linux userspace,
guest root, service management, and nested containers. It does not boot an
unmodified QEMU image or use QEMU TCG. The current host has `/dev/kvm`; the
Apptainer launcher hides it and checks its absence on every launch. This is a
controlled test without KVM access, not yet a run on a second node lacking KVM.

Networking uses a UML vector NIC (`vec0`) and VDE Slirp: guest address
`10.0.2.15`, gateway `10.0.2.2`, DNS `10.0.2.3`. Host loopback ports 22022 and
25901 forward to guest SSH and VNC. Docker keeps its normal guest bridge,
iptables/NAT, and OverlayFS storage. No host TAP, bridge, or firewall changes.

## Completed baseline experiments

| Capability | Observed result | Evidence |
|---|---|---|
| Full Linux boot | systemd, guest root, 4 CPUs, cgroup v2 | `runs/boot4-smp-systemd/systemd-probe.log` |
| Network | outbound DNS/HTTPS, SSH forwarding | `runs/boot3/ssh-probe.log` |
| Docker/Compose | MariaDB + nginx healthy, inter-container DNS/HTTP, published HTTP | `runs/boot5-docker/docker-tests.log` |
| Persistence | SQL row survives Compose down/up | same log, `VOLUME_PERSISTENCE=PASS` |
| Container privileges | memory/CPU cgroup settings and privileged tmpfs mount | same log, `DOCKER_TESTS_PASS` |
| Real Docker inside Docker | inner daemon, bridge/NAT, OverlayFS, nginx HTTP | `runs/boot5-docker/dind-test.log`, `DIND_TEST_PASS` |
| GNOME and input | real desktop; typed commands execute guest sudo/root check | `runs/boot5-docker/desktop-input-test.log`, `desktop-terminal.png` |
| Software graphics | llvmpipe OpenGL 4.5 / GLES 3.2 | same desktop log |

Guest-reported cached-image Compose startup with a freshly initialized database took 18 s;
recreating services with their database volume took 10 s. The inner DinD daemon
started in 23 s. These are individual observations, not latency distributions.
First SMP/systemd boot, before the desktop was installed, reported 45.790 s.
A replay of the finished image reported 12.630 s in systemd-analyze; this excludes
host disk staging and Apptainer startup and is not an end-to-end launch timing.

### Performance measurements

Three trials per mode, identical compiled C executable, arithmetic checksums
matched. Native execution was pinned to four permitted CPUs; UML used four
virtual CPUs. Measurements preceded GNOME startup. No simultaneous KVM baseline.

| Test | Native median | UML median | UML/native |
|---|---:|---:|---:|
| Single arithmetic worker | 0.317604 s | 0.319367 s | 1.006 |
| Four arithmetic processes | 0.322404 s | 0.326909 s | 1.014 |
| Four arithmetic threads | 0.325716 s | 1.289606 s | 3.959 |
| 20,000 raw getpid syscalls | 0.004925 s | 0.208236 s | 42.28 |

See `scripts/bench.c`, `runs/benchmarks/`, and its `summary.json`. These tests
show promising native arithmetic execution and serious syscall/threading costs;
they do not establish end-to-end browser, Moodle, or benchmark task speed.

## Replaying the saved lab

For the application configuration that has exercised all three target apps,
from an allocated node with the prerequisites below:

```bash
cd ~/scratch/general-vm
LAB_BASE_IMAGE=images/ubuntu-uml-ready.ext4 \
LAB_KERNEL_IMAGE=/lab/tools/uml-up/bin/linux LAB_MODULE_PROFILE=uml-up \
LAB_CPUS=1 LAB_SECCOMP=on LAB_DISK_MODE=raw scripts/start-lab.sh
```

This foreground command stages the saved ext4 image on node-local `/tmp`, starts
a new independent raw disk, and writes its console to a new `runs/replay-*`
directory. It holds a lock and refuses occupied loopback ports. The command above
uses 4GiB / 1CPU. Launcher defaults without these environment variables retain
the historical SMP4 kernel and original base image. In a second terminal,
once SSH is ready:

```bash
cd ~/scratch/general-vm
scripts/ssh.sh
# Or run commands directly:
scripts/ssh.sh 'docker compose -f /mnt/lab/scripts/compose.yaml up -d --wait'
scripts/ssh.sh /mnt/lab/tools/seccomp-trap
scripts/ssh.sh bash /mnt/lab/scripts/test-docker.sh
scripts/ssh.sh bash /mnt/lab/scripts/test-dind.sh
scripts/ssh.sh bash /mnt/lab/scripts/test-desktop.sh
scripts/ssh.sh bash /mnt/lab/scripts/test-firefox-headless.sh
scripts/ssh.sh 'sync; systemctl poweroff'
```

The Docker test recreates only this disposable lab's Compose database volume.
Run scripts from files inside the guest: piping a script into `bash -s` lets
`docker compose exec` consume the script's stdin and can end a test prematurely.
Check each terminal PASS marker and the command exit status.

To launch Firefox, run
`scripts/ssh.sh bash /mnt/lab/scripts/start-firefox-gui.sh` after starting Compose.
The headful input check is `scripts/test-firefox-headful-input.sh` inside the
guest. It expects a maximized Firefox window and the welcome dialog accepted.
It verifies actual input callbacks and sandboxed content processes; inspect its
screenshots as well. Test timing includes deliberate UI waits and is not a speed
benchmark. `LAB_FIREFOX_BINARY` and `LAB_FIREFOX_PROFILE`, passed inside the
guest, select isolated browser-version comparisons.

The VNC desktop is on **the compute node's** `127.0.0.1:25901`, test password
`labvnc01`; guest `ga` has passwordless sudo. An SSH tunnel is needed to view it
from another machine. The lab SSH key is generated specifically for this image.
All credentials and database data here are disposable test fixtures.

The original base `images/ubuntu-uml-lab.ext4` and initialized application base
`images/ubuntu-uml-ready.ext4` are 16GiB apparent size, sparse. Changes remain in
the per-run node-local raw or COW file, not in the selected base. A subsequent
launch starts from its base; it does not resume the previous run's disk.
Stop with guest `systemctl poweroff` and let the foreground launcher exit.

## Kernel configuration and browser investigation

Debian's UML 7.1um1 supplied a useful starting configuration, but had SMP and
several nftables families disabled. `notes/uml-smp-7.1.3.config` enables SMP,
NR_CPUS=8, the IPv4/IPv6/inet nftables families, NAT, legacy iptables, and
SquashFS xattrs. Missing nftables families caused Docker's original startup
failure; they were enabled instead of bypassing Docker's firewall.

Firefox initially crashed sandboxed subprocesses. A diagnostic with browser
sandboxes disabled rendered nginx, but that is not counted as normal operation.
A standalone seccomp trap reproducer then exposed incorrect SIGSYS metadata:

```text
Native: si_call_addr == ucontext RIP, exit 0
UML:    si_call_addr=0x6005d30a, ucontext RIP=0x401688cd, exit 1
```

UML's `KSTK_EIP` reads the saved kernel switch context; generic seccomp code
uses it as the user instruction pointer. The experimental patch in
`patches/0001-uml-seccomp-user-instruction-pointer.patch` uses the task's user
register IP. The original kernel is retained under
`tools/uml-smp/bin/linux-net-before-seccomp-fix`. The baseline failing reproducer
logs and Firefox strace summary are under `runs/boot5-docker/`.

The Chromium trap handler used by Firefox's sandbox infrastructure checks the
signal instruction pointer against its context; see the upstream
[trap handler](https://chromium.googlesource.com/chromium/src/+/lkgr/sandbox/linux/seccomp-bpf/trap.cc).
The IP fix alone exposed a second problem: `syscall_rollback()` leaves -ENOSYS
in the signal context instead of the syscall number. Patch 0002 restores that
register. Both fixes together pass the expanded reproducer and sandbox-enabled
headless Firefox rendering. See `notes/seccomp-findings.md` for the complete
before/after evidence. These are experimental patches, not upstream-reviewed.

The successful replay is `runs/replay-20260906T183619Z-579690/`: it verifies the
saved SQL row after VM restart, the seccomp fix, and headless Firefox. Those
combined checks took 17.00 s of host wall time, excluding guest boot. Actual
Docker inside Docker passed again on this kernel. A visible Firefox content
process reports NoNewPrivs=1 and Seccomp=2, but that GUI run subsequently aborted
after clock_gettime returned EFAULT. Interactive browsing is not counted as
passed from that run. Its journal and trace archive are preserved. A final single-CPU comparison
(`runs/replay-20260906T184324Z-702099/`) rendered the page behind the welcome
dialog but became unresponsive during interaction; reducing to one CPU did not
establish a usable browser session.

Rebuild locally with `scripts/build-kernel.sh`. It extracts the saved upstream
source if necessary, applies both patches, uses the saved configuration, builds
with `-j8`, and installs only under this directory. It replaces the executable
using a new inode, so an existing executable is not overwritten in place.

## Scope and prerequisites

This node permits unprivileged user/mount/network namespaces, UML's process
tracing, and executable shared-memory mappings. Those are separate prerequisites
from KVM and need verification on other nodes. Apptainer and its configuration
are also part of the tested host environment. The Debian container supplies a
newer glibc than the host, without host installation.

The test uses a Linux-specific replacement kernel and requires guest boot,
network and module adaptation. It has not validated Windows, Android, physical
GPU acceleration, audio, device passthrough, memory snapshots, or the project's
original environment images/verifiers. The complete independent Moodle deployment
has now been exercised. Docker reports no cpuset support with this configuration;
CPU quota and memory settings were checked, but enforcement under sustained load
was not benchmarked.
The scratch hostfs sharing and isolation setup have not had a security audit.

All durable inputs, binaries, scripts, logs and images are in this directory.
`runs/local` points to node-local temporary builds and disks. Downloaded sources,
package archives, initial and final configs, image checksums, and intermediate
failures are retained. `notes/experiment-scope.md` gives the experiment history.

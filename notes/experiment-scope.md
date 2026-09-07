# Independent UML experiments

Started 2026-09-06 on babel-u5-28, inside the existing Slurm allocation.
No gym-anything source, images, scripts, or runtime are used.
No host sudo, system package installation, or administrator changes.

Host: AlmaLinux 5.14.0-687.25.1.el9_8.x86_64; real UID/GID 2710891.
Host /dev/kvm exists, but each UML launch uses Apptainer --userns
--containall and asserts /dev/kvm is absent before executing the kernel.
Guest memory uses the container's /dev/shm. Active build/disk files are
in node-local /tmp, linked by runs/local; their path is in runs/local-path.txt.

Inputs:
- Debian user-mode-linux 7.1um1 (kernel 7.1.3). Extracted, not installed.
- Debian trixie-slim Apptainer image for its newer glibc.
- Ubuntu Jammy cloud filesystem, build 20260829. SHA256 verified.
- Upstream Linux 7.1.3 source. SHA256 verified.
- VDE/Slirp Debian packages, plus existing host Slirp/GLib/PCRE libraries.

Initial findings:
- Host unprivileged user, mount and network namespace creation succeeds.
- Debian kernel config enables seccomp, but disables SMP (NR_CPUS=1).
- First guest boot succeeded with seccomp=on and guest root.
- Guest nested PID/mount namespaces succeed. Guest /dev/kvm is absent.
- Boot1 used a disposable COW file and shut down cleanly.

Further findings:
- Stock Ubuntu booted with systemd PID 1 on a locally compiled SMP UML
  kernel; nproc reports 4 and /sys/fs/cgroup is cgroup2fs.
- DNS/outbound HTTPS and localhost host-to-guest SSH forwarding work.
- systemd-analyze reported 45.790s for the first SMP/systemd boot.
- Docker 29.1.3 initially failed because the inherited Debian UML kernel
  config disables NF_TABLES_IPV4/IPV6/INET. This was reproduced with a
  direct `iptables -t nat -N` operation; no firewall bypass was applied.
- The next kernel config enables those families, NFT_NAT, legacy
  iptables, and SquashFS xattrs. See the saved configurations and logs.
- Image resizing required e2fsck before resize2fs. The root filesystem is
  now 16 GiB. A VDE wrapper handles '=' in Slirp forwarding options,
  which UML's kernel-command-line vector parser does not accept.

Docker validation with the corrected kernel (boot5):
- Docker 29.1.3 starts under systemd using the containerd OverlayFS
  snapshotter and normal iptables rules (no --iptables=false or VFS fallback).
- MariaDB 10.11 + nginx Compose services become healthy in 18 seconds
  with images already cached; recreation with the database volume takes 10s.
- Published localhost HTTP, container-to-container DNS/HTTP, a SQL write,
  and SQL data survival after Compose down/up all pass.
- A container sees memory.max=134217728 and cpu.max='50000 100000'.
- A privileged container can mount and use a guest tmpfs.
- Final script marker DOCKER_TESTS_PASS is present.
- An initial harness attempt ended early because docker compose exec
  consumed the bash -s input stream. Tests were rerun from a script file
  in the guest and the terminal success marker was verified.

Actual Docker-in-Docker validation:
- A privileged docker:29-dind container, with /var/lib/docker on a named
  guest volume, started its own Docker daemon in 23 seconds.
- The inner daemon pulled and ran nginx, created its own bridge/NAT,
  and served HTTP on its published port inside the outer container.
- Inner storage reports overlayfs and cgroup v2; inner container root
  works and /dev/kvm remains absent. Marker DIND_TEST_PASS is present.
- The temporary DinD container was removed after the test.

CPU microbenchmarks (three trials, identical C executable, before starting
GNOME; native process pinned to four permitted physical CPUs; UML ncpus=4):
- Single arithmetic worker: native median 0.317604s, UML 0.319367s (1.006x).
- Four arithmetic processes: native 0.322404s, UML 0.326909s (1.014x).
- Four arithmetic threads: native 0.325716s, UML 1.289606s (3.959x).
- 20,000 raw getpid syscalls: native 0.004925s, UML 0.208236s (42.28x).
Checksums agree in every arithmetic trial. These are small mechanism
benchmarks on this allocation, not a KVM comparison or full-task timings.

Desktop investigation:
- The first minimal GNOME installation produced a session failure.
- Missing recommended packages included the gjs executable needed for
  GNOME's D-Bus services, accessibility, settings, SVG and cursor support.
- The manual dbus-run-session launch also lacked a proper logind session.
- The next attempt uses the packaged TigerVNC systemd/PAM session service
  and the additional GNOME dependencies. Screenshots are retained.

Further desktop and browser results:
- A full GNOME 42.9 session works through the packaged TigerVNC PAM service.
- Actual keyboard input into GNOME Terminal executes guest `id`, `sudo -n id`,
  and the no-KVM check; DESKTOP_INPUT_TEST_PASS is present and its screenshot
  was visually inspected. Explicit input focus was needed in the test harness.
- Mesa 23.2.1 llvmpipe provides OpenGL 4.5/GLES 3.2 in this VNC session.
- Firefox 155.0.1 initially crashes sandboxed subprocesses. Correcting the
  headless process's XDG_RUNTIME_DIR did not fix it. With all six browser
  sandboxes disabled as a temporary diagnostic, the same headless nginx
  page renders successfully. This is not counted as a normal browser pass.
- strace shows SIGSYS delivery followed by exit_group(1). The signal's
  si_call_addr is 0x6005d30a, a saved UML kernel context address.
- scripts/seccomp-trap.c isolates the issue without Firefox: the host reports
  si_call_addr == ucontext RIP; UML reports different addresses and exits 1.
- Linux 7.1.3 arch/x86/um/asm/processor.h defines KSTK_EIP using the saved
  kernel switch buffer. Generic seccomp and SIGSYS generation use this macro
  as the user instruction pointer. An experimental one-line patch changes
  KSTK_EIP to the task's user register IP. Baseline kernel is retained as
  tools/uml-smp/bin/linux-net-before-seccomp-fix; patch under patches/.
- A replay of the saved image with the IP fix passes the instruction-pointer
  assertion and preserves the SQL row across VM shutdown/reboot. Firefox still
  fails. Expanding the reproducer reveals a second defect: the signal context
  RAX contains -38 (-ENOSYS) instead of syscall 39, whereas native Linux returns
  syscall 39 in that context. UML's syscall_rollback() is a no-op.
- patches/0002-uml-restore-syscall-number-on-rollback.patch restores the syscall
  number to the return register. The IP-only kernel and its failed browser log
  are preserved. The next boot tests both fixes together.
- The first saved-image replay reports 12.950 s in systemd-analyze (guest clock;
  excludes staging the disk and starting Apptainer).

Final experiment round:
- The two-patch kernel passes the expanded seccomp reproducer with both IP and
  syscall-number matches. Saved-image replay preserves SQL data and passes
  normal sandbox-enabled headless Firefox rendering. The replay checks take
  17.00 seconds of host wall time. A separate DinD retest also passes.
- Four-CPU GUI Firefox renders the Docker page with content-process Seccomp=2
  and NoNewPrivs=1, but aborts with `clock_gettime failed: 14` (EFAULT). Tracing
  clock syscalls changes timing and does not reproduce that error cleanly.
- A single-CPU comparison renders the page behind Firefox's welcome dialog,
  then becomes unresponsive during interaction. Interactive Firefox is NOT a
  pass in this round, and this does not establish a simple SMP-only cause.
- Screenshots were inspected; the GNOME terminal input test passes separately.
- Saved base image has been replayed successfully. Kernel and patch checksums
  validate. All shell scripts parse. No original repository code was involved.

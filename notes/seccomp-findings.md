# UML seccomp signal defects reproduced in Linux 7.1.3

These are local experimental fixes, not upstream-reviewed patches.

`tools/seccomp-trap` installs a filter trapping getpid (syscall 39), then compares
the SIGSYS metadata with the delivered ucontext. It is built from
`scripts/seccomp-trap.c` with host GCC and run unchanged on host and guest.

| Kernel | Signal instruction address | Context RAX | Result |
|---|---|---|---|
| Host 5.14 | matches ucontext RIP | 39 | pass |
| Original UML 7.1.3 | saved kernel context, not user RIP | not checked in initial test | fail |
| UML with patch 0001 only | matches user RIP | -38 (-ENOSYS) | fail expanded test |
| UML with patches 0001 + 0002 | matches user RIP | 39 | pass expanded test |

1. `arch/x86/um/asm/processor.h`: KSTK_EIP obtains the instruction pointer from
   the kernel switch buffer. Generic `populate_seccomp_data()` and
   `force_sig_seccomp()` use it for userspace instruction-pointer information.
   Patch 0001 reads the task's user register IP instead.
2. `arch/um/include/asm/syscall-generic.h`: syscall_rollback() does nothing.
   UML sets the return register to -ENOSYS before secure_computing(), so that
   value remains in the SIGSYS context. Patch 0002 restores the syscall number,
   as needed when a seccomp trap returns control to userspace.

Firefox's Chromium-derived trap infrastructure validates the instruction
pointer and syscall number before dispatching a trap. See the upstream
[Chromium handler](https://chromium.googlesource.com/chromium/src/+/lkgr/sandbox/linux/seccomp-bpf/trap.cc)
and [Mozilla sandbox overview](https://wiki.mozilla.org/Security/Sandbox/Seccomp).
The source inspection motivated the tests; the local failures and fixes are
independently demonstrated by the executable and logs.

The two-patch kernel passes the seccomp reproducer, normal sandbox-enabled
headless Firefox rendering of Docker nginx, Compose/SQL persistence after VM
replay, and actual Docker inside Docker. Its visible Firefox content process
also reports NoNewPrivs=1 and Seccomp=2. A subsequent GUI clock_gettime/EFAULT
abort is a separate unresolved observation and is not fixed by these patches.

Evidence:
- `runs/boot5-docker/seccomp-trap-native.log`
- `runs/boot5-docker/seccomp-trap-uml-before.log`
- `runs/boot5-docker/firefox-strace-summary.log` and `firefox-strace.tar.gz`
- `runs/replay-20260906T183304Z-510429/seccomp-trap-native.log`
- `runs/replay-20260906T183304Z-510429/seccomp-trap-uml-ip-fix.log`
- `runs/replay-20260906T183619Z-579690/replay-test.log`
- `runs/replay-20260906T183619Z-579690/firefox-sandbox-enabled.png`
- `runs/replay-20260906T183619Z-579690/firefox-sandbox-status.log`
- `runs/replay-20260906T183619Z-579690/firefox-gui-journal.log`

Baseline, IP-only, and final kernel executables are retained under
`tools/uml-smp/bin/`; final hashes are in `notes/seccomp-fix-sha256.txt`.

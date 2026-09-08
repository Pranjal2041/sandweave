# Native Windows kernel execution without KVM: mechanism experiments

Date: **2026-09-08**. Constraint: an actual Windows VM here, fast enough for the
broader VR workload, with **no KVM, no TCG and no host sudo or administrator
changes**. Changing the engine is within scope. Wine, an NT-compatible
replacement kernel, a remote Windows machine, and a slow emulated boot do not
silently replace that objective.

**Five routines extracted from Microsoft's Windows 11 kernel execute correctly
inside the existing gVisor systrap sandbox after a restricted memory-access
rewrite. Windows itself has not booted.** The experiment establishes a useful
part of a possible engine, not a VM implementation or a game performance result.

The original transcript's relevant lesson is that a packaged engine's missing
features are implementation questions. Earlier Linux success required actual
engine changes while retaining the complete environment. This investigation
therefore examines instruction execution, addresses, boot handoff, memory
mapping and device transport separately. The earlier
[research survey](windows-without-kvm-research.md) remains useful background.

## Execution design under investigation

```text
Windows applications: native x86-64 application instructions
             |
      Windows syscall / exception entry
             |
Microsoft NT kernel: cached, selective x86-64 instruction rewriting
  ordinary computation remains native
  memory references target aliases of the guest's memory
  privileged / sensitive operations update virtual CPU and device state
             |
Linux userspace memory, threads and I/O inside the gVisor isolation boundary
```

This avoids sending all application instructions through a general CPU
translator. A full implementation still needs virtual CPU state and faithful
hardware behavior. Selecting a different emulator name does not establish this
execution model or its performance.

### Historical implementation evidence

FAUmachine's 2004 implementation executed applications directly and cached
kernel code containing original instructions plus simulator calls. It used
Unix signals for exceptions and `ptrace` or a host patch for syscall dispatch.
Its paper explicitly discusses instructions such as `POPF` whose privileged
effects fail silently in userspace, code-cache invalidation, and preserving
original return addresses. The measured kernel compile took 635 seconds with
the patched dispatch and JIT versus 141 seconds native. Windows compatibility
was unfinished. This is architectural precedent, not evidence of adequate
current performance.
[Authors' paper](https://admingilde.org/~martin/papers/linuxkongress2004.pdf).

The inspected Debian source archive, `faumachine_20180503.orig.tar.xz`, has
SHA256 `90bd3664bb1c214580c0e078fade7805b64bb3fa4cacef9b0319cd55797312aa`,
verified against its package metadata. Its `NEWS` says Windows XP support used
the QEMU simulator in March 2005 and that this became the default in July.
`chips/arch_x86/cpu_jit_compile.c` is a QEMU-derived CPU translator. This archive
does not provide the desired native Windows execution backend; it was inspected,
not launched.
[Debian archive](https://archive.debian.org/debian/pool/main/f/faumachine/),
[source metadata](https://archive.debian.org/debian/pool/main/f/faumachine/faumachine_20180503-4.dsc).

Captive NTFS provides a different concrete precedent: its author describes
loading original `ntoskrnl.exe` code into Unix userspace, wrapping selected
functions and replacing hardware-dependent functionality. It deliberately
skipped kernel initialization and did not supply a Windows userspace/desktop.
[Author's implementation account](https://www.jankratochvil.net/project/captive/doc/Details.pm).

## Address translation tested on this node

The L40S allocation `10361186`, node `babel-o9-20`, has an AMD EPYC 9354 CPU
with 57-bit virtual-address support. Linux kernel
`5.14.0-687.25.1.el9_8.x86_64` accepted mappings above the ordinary 47-bit
userspace range. The test used allocation CPU 12, no GPU operations, and only
its own anonymous mappings and memory file.

For a guest using 48-bit canonical addresses, consider this mapping, with
addition modulo 2^64:

```text
host alias = guest virtual address + 0x0001000000000000

guest kernel address 0xffff800000100000 -> host 0x0000800000100000
guest user address   0x0000000040000000 -> host 0x0001000040000000
```

The two valid guest address ranges fit within the host's wider userspace.
An FS/GS segment base can perform this addition as part of native memory
address generation. Stored pointers and pointer comparisons need not be
converted to host addresses. This observation does **not** remove the need to
handle guest paging, invalid addresses, stack semantics or protection.
Linux documents explicit high-address `mmap` requests on five-level hosts.
[Linux five-level paging](https://www.kernel.org/doc/html/v5.15/x86/x86_64/5level-paging.html).

[`windows-native-address-probe.c`](../scripts/windows-native-address-probe.c)
mapped a shared page at both shifted addresses, followed a circular linked
list whose pointers retained their original high-half values, and verified
coherent reads/writes through both aliases. The host test passed.

The current Sentry rejects those wider mappings with `ENOMEM`. A restricted
kernel-only variant with bias `0x0000800000000000` maps the tested high-half
pointer to `0x100000`, and **passed inside gVisor**. Its `arch_prctl(ARCH_SET_GS)`
returned `EPERM`; ordinary userspace `WRGSBASE` successfully installed the
base on this host. The value survived Sentry transitions. This does not claim
the same base is usable on a host without five-level addressing.

Seven interleaved samples, each with eight million dependent list iterations:

| Execution | Median rewritten / ordinary duration |
|---|---:|
| Host, restricted kernel alias | 1.2363 |
| Host, wide alias | 1.2361 |
| Existing gVisor, restricted kernel alias | 1.2375 |

This deliberately dependency-heavy load test costs approximately 24% more
with the segment prefix. It is not an overall kernel overhead estimate.

## Actual Microsoft kernel code executed

The input is `ntoskrnl.exe` version **10.0.26100.9278**, fetched directly from
[Microsoft's symbol server](https://msdl.microsoft.com/download/symbols/ntoskrnl.exe/4CA4F63D1450000/ntoskrnl.exe).
The downloaded 13,100,568 bytes match SHA256
`f9215193d514541abdacec7c19e3f8f765894f7abb517b3845a39cf61ddb3066`.
Winbindex supplied the timestamp/image-size lookup metadata. Hash verification
is recorded; no independent Authenticode verification is claimed.

[`prepare-windows-kernel-probe.py`](../scripts/prepare-windows-kernel-probe.py)
uses iced-x86 1.21.0 to follow each selected routine's direct control flow,
retain ordinary instructions, add GS prefixes to explicit data-memory operands,
and re-encode internal branches. It rejects privileged operations, calls,
indirect branches, existing segment overrides, RIP-relative operands and
explicit stack operations. These limits are deliberate and fail closed; this
is not a general NT translator. Original and rewritten code are both executed
from a separate code buffer with the Windows x64 calling convention.
[iced-x86 source](https://github.com/icedland/iced).

| Routine | Reachable instructions | Memory operands rewritten |
|---|---:|---:|
| `RtlInitializeBitMap` | 3 | 2 |
| `RtlSetBit` | 8 | 3 |
| `RtlClearBit` | 8 | 3 |
| `RtlTestBit` | 9 | 2 |
| `RtlSplay` | 154 | 89 |

[`windows-kernel-routine-probe.c`](../scripts/windows-kernel-routine-probe.c)
checked 10,000 bitmap operations against a separate byte-array reference.
For 10,000 splay operations it compared every tree link against the original
Microsoft routine operating on ordinary pointers. It also independently
checked ordering, parent links, absence of cycles, retained nodes and the
returned root pointer. The rewritten tree retained high-half guest pointers
throughout. `RtlSplay`'s documented behavior is to make the selected node the
root while rearranging the tree.
[Microsoft RtlSplay specification](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntddk/nf-ntddk-rtlsplay).

All correctness checks passed on the host and in a fresh, networkless gVisor
sandbox. Seven interleaved timed samples, each 200,000 splay operations:

| Execution | Median rewritten / original duration |
|---|---:|
| Host, restricted kernel alias | 1.0532 |
| Host, wide alias | 1.0522 |
| Existing gVisor, restricted kernel alias | 1.0551 |

The approximately **5.5% overhead inside gVisor applies to this selected
routine**. These functions were selected because their instruction behavior
fits the restricted translator. They do not exercise privileged instructions,
full kernel initialization, interrupts, page-table changes, the scheduler,
drivers, a Windows process or graphics. No Windows boot or near-native VM
performance follows from this result.

## Syscalls and memory mapping: measured integration costs

The address probe intercepted 20,000 synthetic foreign syscalls and verified
every returned value. Each mechanism ran in a separate child process.

| Mechanism | Host | Existing gVisor guest |
|---|---:|---:|
| Syscall user dispatch | 1.871 microseconds/call | `EINVAL`, unsupported guest ABI |
| Seccomp trap + signal return | 1.905 microseconds/call | 20.087 microseconds/call |

The guest measurement pins Sentry and the workload to one allocated CPU; it
does not establish costs under a larger CPU allocation. Neither handler ran a
real Windows syscall. Patching common Windows syscall entry stubs or providing
an engine-level dispatch path could avoid this nested signal route, but neither
optimization is implemented or measured here. Unrecognized invocation sites
would still require a correct fallback.

Host syscall user dispatch is already used internally by systrap
(`pkg/sentry/platform/systrap/subprocess.go:288`). Its presence in systrap does
not implement the same Linux `prctl` interface for an application inside the
Sentry. Linux's documentation describes its userspace-controlled personality
switch and explicitly distinguishes it from a security boundary.
[Linux syscall user dispatch](https://www.kernel.org/doc/html/latest/admin-guide/syscall-user-dispatch.html).

[`windows-shadow-map-probe.py`](../scripts/windows-shadow-map-probe.py)
tested a straightforward way to mirror guest page tables with host `mmap`:

| Guest mappings, 8 MiB total | Host VMAs |
|---|---:|
| 2,048 sequential physical-page offsets | 1 |
| 2,048 permuted physical-page offsets | 2,048 |

Both mappings had correct backing-file alias behavior. The node's
`vm.max_map_count` is **65,530**, already relevant to the
[Alyx failure](preempt-vr-debug.md). If each mapping required a separate VMA,
the nominal limit would represent only about 256 MiB of 4 KiB mappings before
other VMAs are counted. This is a conditional capacity calculation, not an
observation of Windows' actual page layout. A usable engine must coalesce
mappings where possible and handle fragmented layouts without assuming an
administrator will raise this limit.
[Linux VMA limit](https://kernel.org/doc/html/latest/admin-guide/sysctl/vm.html#max-map-count).

The host accepted an unprivileged `userfaultfd(UFFD_USER_MODE_ONLY)` and its
`UFFDIO_API` negotiation, reporting feature mask `0x4fff`. No fault-resolution
or write-protection workload has been validated. Sentry's guest `userfaultfd`
entry currently returns `ENOSYS`. A copied/shadow-memory design using such a
mechanism would still have to preserve alias, DMA and SMP coherence; successful
descriptor creation alone does not solve that problem.
[Linux userfaultfd](https://www.kernel.org/doc/html/latest/admin-guide/mm/userfaultfd.html).

## Boot handoff and remaining engine work

Quibble is an open-source replacement Windows bootloader. Its stated support
extends through Windows 10 22H2; the inspected revision is
`7402412537678e46f40613a7a4a73644a2d0f68c`. Its `src/boot.cpp` constructs versioned
loader parameter blocks, loads the registry and drivers, prepares memory
descriptors and stacks, and ultimately calls `KiSystemStartup`. `src/mem.cpp`
and `src/quibble.asm` still assume privileged paging, descriptor-table and task
register operations. They provide concrete boot-handoff implementation details,
not a userspace Windows boot as supplied. No Quibble boot was attempted.
[Quibble source and supported versions](https://github.com/maharmstone/quibble/tree/7402412537678e46f40613a7a4a73644a2d0f68c),
[boot implementation](https://github.com/maharmstone/quibble/blob/7402412537678e46f40613a7a4a73644a2d0f68c/src/boot.cpp).

The experiments leave these substantive parts unimplemented:

1. **General kernel instruction execution.** Preserve guest return addresses,
   stack values and unwind behavior while relocating code; implement `CR0/CR3/
   CR8`, MSRs, interrupt state, descriptor tables, `SWAPGS`, `SYSRET` and `IRET`.
   Instructions such as `POPF`, segment-state reads and string operations need
   explicit treatment. A prefix is insufficient for implicit ES destinations.
2. **Complete address and protection semantics.** Add an explicit wider Sentry
   address-space mode, resolve native user mappings versus shifted kernel/user
   aliases, preserve canonical-address faults and keep VM-monitor memory
   protected. Keep Windows' own GS/KPCR semantics while using an appropriate
   translation base. The current probe borrows GS only for its bounded call.
3. **Guest page tables and multiple CPUs.** Translate guest physical pages,
   permissions, accessed/dirty state and CR3 changes; invalidate translations
   correctly; handle alias writes, page-table edits, device DMA and SMP without
   exhausting host VMAs. The measured mapping problem makes this a design gate.
4. **A real Windows initialization path.** Supply an actual installation's
   required files, consistent loader structures and virtual hardware, then run
   through kernel initialization into Windows processes. A few native kernel
   routines do not satisfy this step.
5. **Devices and game acceptance.** Implement timers/APIC, storage, input and
   display, then qualify the separate Windows GPU transport and XR runtime.
   The earlier Helios findings may inform graphics; its existing KVM launcher
   cannot be used under this task's constraints.

The evidence supports continued investigation of a custom native-application /
selectively-rewritten-kernel engine. It does not establish that all remaining
pieces will combine correctly or meet VR frame-rate requirements. No current
installable stack satisfying all requirements was established by this research.

## Reproduction and preserved evidence

All generated code derived from Microsoft binaries, downloaded dependencies,
executables and detailed logs remain in ignored `downloads/` and `runs/`.
Authored probes and this record are tracked. Machine-readable source hashes,
all timing samples and actual success/failure results are preserved in
[`windows-native-probe-results.json`](windows-native-probe-results.json).

Prepare the pinned code with `pefile` and `iced-x86==1.21.0` available:

```sh
PYTHONPATH=downloads/windows-native-research/python \
  python scripts/prepare-windows-kernel-probe.py \
  --output runs/windows-kernel-leaves-NEW
```

On the allocated compute node, pin to an allocated CPU (12 in this run):

```sh
taskset -c 12 python scripts/run-windows-native-address-probe.py \
  --runsc tools/runtime-builds/d7c9c0e88768628740fb5c19f1e44a5e90c3bbb72fc56ee9d20a7ad0d42acdf7/runsc

taskset -c 12 python scripts/run-windows-native-address-probe.py \
  --runsc tools/runtime-builds/d7c9c0e88768628740fb5c19f1e44a5e90c3bbb72fc56ee9d20a7ad0d42acdf7/runsc \
  --kernel-leaves runs/windows-kernel-leaves-NEW/kernel-leaves.bin

taskset -c 12 python scripts/windows-shadow-map-probe.py

# Host-only wide-address version of the Microsoft routine experiment:
gcc -O2 -Wall -Wextra -Werror scripts/windows-kernel-routine-probe.c \
  -o runs/windows-kernel-leaves-NEW/probe
taskset -c 12 runs/windows-kernel-leaves-NEW/probe \
  runs/windows-kernel-leaves-NEW/kernel-leaves.bin wide
```

The runner creates a separate networkless guest for each invocation and uses
an explicit immutable runtime. Its normal successful address-probe result
includes unsupported wide guest mappings and unsupported guest syscall user
dispatch: these are reported capabilities, not falsely counted as passes.
The earlier failed launches and the `ARCH_SET_GS` failure before the FSGSBASE
follow-up remain in the logs. The desktop environments and runtime selector
were not changed. No engine source change was made in this investigation.

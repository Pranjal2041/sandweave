# Disk-backed guest memory

Sandweave 0.2.8 adds `Memory(guest="4GiB", disk="16GiB", disk_path="/scratch/my-memory")`.
The guest sees 20 GiB. The worker reserves 4 GiB plus the runtime allowance;
the kernel enforces that host-memory cap. This mode requires a delegated
cgroup-v2 memory controller or Slurm with enforced step memory limits. It
does not promise reduced RAM reservations on hosts that cannot enforce them.

## Storage and lifecycle

`disk_path` is an explicit absolute worker path. Each launch gets a mode-0700
random subdirectory. The engine creates unnamed `O_TMPFILE` files there and
donates their descriptors into the Sentry. Neither the directory nor the
descriptors are exposed as guest mounts or user file descriptors. The files
are disk-backed versions of gVisor's guest page allocator; host Linux performs
paging without guest `swapon`, host sudo, or KVM.

The backing file can consume the full guest budget, not only the additional
disk allowance. Bounded disk allocations reserve filesystem blocks before
mapped writes. Existing RAM-only allocation paths and unbounded disk overlay
allocators retain their previous behavior. The guest page budget still limits
populated pages, while a separate cgroup limits host RAM. Worker control
processes and launch supervision remain outside that cgroup.

Memory restore needs a separate empty file while the initial boot kernel
still exists. The engine donates distinct boot and restore files rather than
truncating a live mapping. Snapshots store guest contents normally. A restore
creates new files, and the SDK permits changing only `disk_path` within the
saved resource configuration. Normal cleanup removes the empty private
directory; the kernel releases unnamed files even if a runtime is killed.

The Slurm supervisor creates only a step in the existing allocation. It
requests a separate memory cap, shares the node's allocated CPUs and binds
the task to the worker's actual CPU mask. The shared CPU broker runs outside
individual memory caps. Startup verifies the dedicated step's effective
limit and disabled host swap before touching guest memory. The alternative
delegated-cgroup path creates a child under an already delegated subtree;
it does not change existing limits or move other processes.

Native Apptainer and shared CUDA MPS partitions are unsupported in this mode.
Disk memory does not extend GPU VRAM or make pinned memory pageable. The
regular-file backing requires filesystem support for unnamed temporary files,
allocation and hole punching; RAM filesystems are rejected.

## Acceptance on 2026-09-11

Tests used the public SDK on `babel-p9-16`, local XFS/NVMe, and independently
capped steps in an existing Slurm allocation. No parent job or unrelated
environment was stopped. The final source-built runtime is
`4ceb6e371f04b63f769d20d1202ee29015dba656680ce0be588040d22d205ea0`,
from engine commit `dd5239ad8db0753e8af7da0ea915df71c5357f7c`.

| Test | Result |
| --- | --- |
| Two concurrent 2 GiB guests with 256 MiB RAM + 512 MiB runtime each | Both wrote and reread 1,408 MiB of application pages; distinct private directories. |
| Live snapshot and restore | Verified captured contents after restoring into another parent directory under the same RAM cap. |
| Filesystem snapshot restored with RAM-only resources | Preserved the saved files and cleared the previous disk-memory binding. |
| 20 GiB guest with 4 GiB RAM + 512 MiB runtime | Wrote and reread 18 GiB successfully. |
| Normal termination | All recorded private subdirectories removed. |
| Owner Python process killed with SIGKILL | Automatic cleanup released its backing directory. |
| RAM filesystem supplied as disk storage | Rejected with the cause; no private directory left behind. |
| Worker restricted to CPUs 28–29 | Slurm launch preserved that subset instead of selecting the allocation's first CPUs. |

All successful paging cases recorded major page faults, zero host swap and
zero OOM kills. Kernel `memory.peak` was one 4 KiB page above `memory.max` in
these tests; this is the observed enforcement granularity, not a claim of
byte-exact instantaneous accounting. The 20 GiB test's limit was
4,831,838,208 bytes. Its peak was 4,831,842,304 bytes.

The delegated-cgroup selection and rejection paths have host unit coverage;
the live enforcement backend tested here was Slurm. The earlier
[performance report](disk-memory-profiling.md) measures native paging access
patterns; these SDK acceptance checks establish functionality and cleanup,
not a universal application slowdown.

Reproduce with an explicitly selected disposable worker and independent disk
root, using `tests/integration/test_disk_memory_live.py`. Set `SANDWEAVE_DISK_PATH`
for the small tests, `SANDWEAVE_DISK_LARGE=1` for the 20 GiB case, and optionally
`SANDWEAVE_DISK_RECEIPT` for JSON evidence. The retained
[acceptance receipt](disk-memory-acceptance-20260911.json) contains resource
settings, kernel counters, released directories and engine hashes. It omits
worker credentials and snapshot databases.

# CPU, memory and GPUs

Override resources at creation time. You do not need a new template:

```python
from sandweave import Sandbox

with Sandbox(cpu=4, memory="8GiB") as env:
    print(env.run("python --version").stdout)
```

The same arguments work with `target="lab"` or a printed cluster address.

## CPU sharing

```python
from sandweave import CPU, Sandbox

with Sandbox(cpu=CPU(vcpus=2, weight=200, quota=1.5)) as env:
    print(env.info["cpu"])
```

| Setting | Meaning |
| --- | --- |
| `vcpus` | Virtual CPU count advertised to the sandbox. |
| `weight` | Relative share of CPU time under contention; default 100. |
| `quota` | CPU-time budget measured in cores; `1.5` allows about 1.5 cores of CPU time. |

gVisor enforces weights and quotas through a sampled userspace controller.
These settings share the worker's eligible CPUs; they do not reserve dedicated
physical cores or grant more CPUs than the host allocation supplies.

A busy sandbox can borrow CPU time left unused by other sandboxes, including
peers that are still doing some work. For example, on a four-CPU worker, if one
equally weighted sandbox uses one CPU, another can use the remaining three.
Weights determine shares when there is enough runnable work to compete for
them. Explicit quotas still limit consumption when spare capacity is available.

`vcpus` controls the guest's reported CPU count and gVisor's execution concurrency
per address space. Separate guest processes can together use more host CPUs
than this count. It is neither a sandbox-wide CPU-time ceiling nor a request to
resize an application's thread pool automatically. Use `quota` for a CPU-time
ceiling.

The controller samples CPU time and runnable threads. Demand estimates use
unthrottled observation windows of about 100 ms; scheduling normally updates
every 20 ms. It preserves observations across its own pauses and distinguishes
brief wakeups from threads that keep waiting for CPU. Changes in demand take
time to observe, and short overshoot is possible. Runtime and transport CPU
are counted, but only guest execution is
throttled. These are not hard host cgroup limits.

## Guest and runtime memory

```python
from sandweave import Memory, Sandbox

with Sandbox(memory=Memory(guest="4GiB", runtime="1GiB")) as env:
    print(env.info["memory"])
```

**Guest memory** is the budget for sandbox application pages. **Runtime memory**
is a separate guard for the processes implementing the sandbox. Both count
toward cluster admission. Neither is the host machine's total memory.

A string such as `memory="8GiB"` overrides guest memory and retains the template's
runtime budget. Sizes can also be supplied as integer byte counts.

A 32 GiB worker fits three sandboxes configured with 8 GiB guest memory and
512 MiB runtime memory. With 256 MiB for each budget, it fits up to 64, subject
to its slot limit and other reservations. Slots do not grant additional memory.

Worker budgets are capped at visible cgroup hard limits, including narrower job
steps and ancestor limits. An environment variable cannot raise that ceiling.
If several workers or unrelated processes share a memory allocation, their
budgets still need to fit within that shared allocation.

## Disk-backed memory

```python
from sandweave import Memory, Sandbox

memory = Memory(
    guest="4GiB",
    disk="16GiB",
    disk_path="/scratch/my-memory",
)
with Sandbox(memory=memory) as env:
    print(env.run("free -h").stdout)
    print(env.info["disk_memory"])
```

Applications see 20 GiB of guest memory and allocate it normally. They need no
special calls or awareness of a second memory tier. Linux caches the backing
file in RAM and reclaims its pages to disk under pressure. There is no timer
for borrowing memory, and the guest does not show this storage as Linux swap.

`guest` is the RAM allowance; `disk` adds to the guest's total page budget.
`runtime` defaults to 512 MiB and retains its separate guard. The kernel caps the sandbox's host memory
at `guest + runtime`, including runtime processes and file cache. The exact
portion resident as guest pages depends on runtime overhead. Weave reserves
that RAM amount, rather than `guest + disk + runtime`.
Worker control processes and launch supervision remain outside the sandbox cap;
leave capacity for those and other host processes when setting worker budgets.

`disk_path` is required and names an absolute directory **on the worker**.
Sandweave creates it if needed, then creates a private random subdirectory per
sandbox. It does not derive this path from `SANDWEAVE_HOME`, a cache, or `/tmp`.
`env.info["disk_memory"]` reports the actual directory and host limit in bytes.
The backing files have no filenames and are not mounted into the guest.
Their storage is released when the runtime exits, including a killed runtime.
Normal cleanup also removes the empty private directory.

The backing file covers the entire guest page budget, including pages cached
in RAM. Allow space for **20 GiB**, plus filesystem overhead, in this example.
Storage grows as pages are populated. This space is separate from disk space
used by images, checkpoints and caches. A full filesystem can cause allocation
failures, just as reaching the guest memory budget can.

This mode requires:

- gVisor and a disk filesystem supporting unnamed temporary files and allocation/
  hole punching, such as local ext4 or XFS. RAM filesystems cannot provide overflow.
- A delegated cgroup-v2 memory controller, or a Slurm allocation whose steps
  enforce memory limits. Sandweave creates a separate capped step automatically
  on Slurm and preserves the worker's CPU pool. It does not enable host swap,
  use sudo, or change existing allocations.

Hosts without either memory-control path cannot enforce this mode. Ordinary
RAM-only sandboxes retain their existing requirements. Native Apptainer and
shared CUDA MPS partitions do not support disk memory.
Disk memory extends guest CPU memory, not GPU VRAM; pinned memory still needs RAM.

The same `memory=Memory(...)` argument works with pools. Each sandbox gets its
own backing file, even when pools share image caches. All eligible workers must
provide the selected path and memory-control support.

Memory snapshots preserve guest contents. Restores create fresh backing files;
to restore on another disk, pass the same `Memory(...)` sizes with a different
`disk_path`. Filesystem caches retain files but restart application processes.

Disk paging is workload-dependent. On the measured scratch NVMe, a workload
with 99% of accesses in a warmed 2 GiB region averaged about 5.2 times the RAM
baseline; random access across 20 GiB cost about 260 times as much. See the
[profiling report](https://github.com/Pranjal2041/sandweave/blob/main/notes/disk-memory-profiling.md).

## Use a GPU

```python
from sandweave import Sandbox

with Sandbox(template="cuda", gpu=True) as env:
    print(env.run("nvidia-smi").stdout)
```

Use a model name to select among eligible devices:

```python
with Sandbox(template="cuda", gpu="L40S") as env:
    print(env.info["gpus"])
```

The worker must already have access to that GPU. Weave currently reserves whole
GPUs for cluster admission; fractional GPU admission is not implemented.

## Inspect configuration

```python
from pprint import pprint
from sandweave import Sandbox

with Sandbox() as env:
    pprint(env.info)
```

The summary includes identity, state, template, worker hostname, CPU and memory
settings, selected GPU devices, and VNC details where applicable. Resource
settings are budgets, not current usage. The [dashboard](dashboard.md) shows
live measurements separately.

## Choose a runtime

gVisor is the default. For code using the host kernel directly:

```python
with Sandbox(runtime="apptainer") as env:
    print(env.run("python --version").stdout)
```

Native Apptainer supports commands, files, GPU exposure, pause/resume, and
filesystem caches. It uses the host network and does not support CPU weights,
CPU quotas, offline networking, or memory snapshots.

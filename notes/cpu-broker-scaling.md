# CPU broker scaling and failure recovery

The old broker rediscovered every registered process tree every 250 ms and
read process statistics on its 20 ms scheduling tick. Cost grew with host stub
processes, not just sandbox count. A synchronous control RPC could also stall
the shared loop. Launchers treated a three-second-old broker heartbeat as a
reason to kill their sandboxes. These were Sandweave defects.

## Accounting

Runtime 2026.09.25.1 exposes a fixed-size `CPUControl` response containing actual
host CPU nanoseconds, sample age, runnable task count and pause state. It uses
Linux process CPU clocks for systrap's syscall and execution processes, plus
the Sentry's own process clock. Guest CPU clocks are estimates based on virtual
CPU counts and are not suitable for this controller.

Systrap registers address spaces when created, reused or awakened. A background
sampler visits active address spaces; sleeping ones leave its sampling set after
100 ms for their spinning stubs to park. The cumulative sample is published
atomically. Reading it and the kernel's existing runnable counter does not walk
guest task lists. This works without host cgroup delegation on supported Linux
kernels, including 5.4. Sampling inside the runtime still scales with active
execution processes; the broker's per-sandbox response and processing do not.

Each sandbox has an independent, persistent asynchronous connection with one
request in flight. Slow RPCs cannot occupy the broker loop. The broker checks
launcher identity and a small explicit set of helper PIDs separately; it never
expands them into guest descendants. Helper CPU deltas exclude reaped-child
accounting and handle PID reuse. Weighted demand allocation and quotas retain
their existing sampled, userspace semantics.

## Failures and lifecycle

A broker-owned pause expires in the runtime after 500 ms without renewal.
After three seconds of lost controller health, the launcher revokes its
registration and permanently disables CPU control for that runtime. This
releases weights and quotas without destroying guest processes. Late broker
requests cannot reinstate a revoked pause. Explicit user pauses remain
independent. `env.info["cpu_control"]` reports the degraded state and reason.

Shutdown drains control tasks. A failed peer is isolated, and completed tasks
are removed rather than accumulating for the lifetime of the broker.
Checkpointing still suspends CPU control before saving guest state.

SDK 0.2.29 automatically upgrades prepared engines lacking the new capability
while retaining guest images. Existing workers and running sandboxes keep their
original code. Memory snapshots must retain their pinned engine; old engines
therefore use compatibility process accounting on four bounded executor threads
at 250 ms intervals. That path can still scale with process count, but cannot
block scheduling of updated runtimes. Old engines rely on the launcher to
release pauses and do not gain the new runtime's 500 ms lease.

## Qualification

Tests use disposable workers on four inherited host CPUs, with no host sudo,
KVM or GPU. Host CPUs are not reserved exclusively. Initial fixtures using
240 Python children exceeded their 256 MiB guest memory limit, confirmed by
the runtime's page-fault OOM logs. The scale workload instead compiles small
native sleepers inside the guest, retaining the 256 MiB guest and 256 MiB
runtime settings. It does not disable memory enforcement.

The 36-sandbox run created 17,821 host processes. Broker use was 0.150 CPU
before the sleepers and 0.187 CPU afterward. Across 80 commands, median latency
was 34 ms, maximum latency was 105 ms, and the oldest broker report was 263 ms.
With two busy guests, the four host CPUs were 95.4% utilized. The other 34
runtime trees consumed about 0.96 CPU. Aggregate runtime measurements totaled
3.458 CPUs; a separate boundary-only host process measurement read 3.459 CPUs.
These are measured end-to-end costs, not an assertion that idle sandboxes cost
nothing or that this synthetic workload covers every application.

Pair tests measured 1.93/1.99 CPUs at equal weights and 2.93/1.00 CPUs at 3:1
weights. A 1.5-CPU quota measured 1.52 CPUs. Borrowing beside a partially active
peer, sixteen intermittent peers, and explicit pause/resume passed.

Freezing and killing a broker while a guest was throttled both preserved guest
state and released the pause; the full command completed in 0.57–0.61 seconds.
Late requests stayed fenced, and explicit pause/resume still worked. Freezing
one Sentry left the other sandbox's commands below 28 ms and broker report age
below 261 ms; both sandboxes survived after the Sentry resumed.

Regression tests live in `tests/test_cpu_broker_scale.py` and
`tests/integration/test_cpu_broker_scale_live.py`. The independent host counter
reads in the scale test occur only at measurement boundaries. They are not
part of the running broker. Local raw evidence is retained under
`/scratch/pranjala/sandweave-cpu-scale-20260925`; summarized results without
connection credentials are committed alongside this note.

The clean release build passed the four scale/failure cases, five demand-sharing
cases and eight filesystem/memory snapshot cases. A real older pinned engine
also passed compatibility accounting, quota pauses and broker-death recovery.
That check caught and fixed a compatibility sampling-window bug before release.

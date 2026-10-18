# CPU broker scaling and failure recovery

## Cancellable scheduling stops, 0.2.43

The 500 ms recovery guarantee below was incomplete in earlier engines.
`CPUControl` and `SetCPUPaused` called the full `Kernel.Pause()` while holding
the same mutex needed for resume. That call waits for all task and asynchronous
I/O goroutines. The lease timer was installed only after the wait. A task that
never acknowledged an interrupt therefore blocked both cancellation and lease
recovery. Source inspection confirms this defect; the client's specific
RabbitMQ thread and lost-signal origin were not independently inspected.

CPU scheduling now uses `Kernel.RequestCPUPause()`, which installs an external
stop without waiting for quiescence. Full `Pause()` still waits and remains the
barrier for explicit user pauses and snapshots. The controller owns one nested
stop, so releasing it cannot release someone else's pause.

The existing runnable-task counter checks scheduling progress without a new
process scan. A stop that has not quiesced within 500 ms is released, CPU control
is permanently disabled for that runtime, and its reason reaches the broker's
degraded report. Acknowledgement has an absolute deadline independent of lease
renewals. Healthy acknowledged pauses retain their renewable 500 ms lease.
Generation checks fence stale timer callbacks and late requests after disable.
No goroutine waits for a scheduling stop to complete.

Prepared engines upgrade for new launches through the `cancellable_cpu_pause`
capability. Existing sandboxes and pinned memory snapshots retain their engine;
recreate or use a filesystem restore to move to this runtime.

Acceptance on Babel u9-24, allocation 10689224, without host sudo/KVM/GPU:

- The released 0.2.42 runtime timed out its CPU pause RPC when an owned, busy
  application executor was host-stopped. Cleanup resumed that executor.
- The fixed engine passed kernel and CPU-controller unit tests, including
  unacknowledged stops, cancellation, nested stops, stale timers, permanent
  disable, healthy renewal and lease expiry. The broader boot suite encountered
  mount-fixture permission failures; it is not reported as passing.
- The complete host suite passed: 834 tests, four skips. Prepared-installation
  fixtures were updated for the new required runtime capability, and the
  upgrade test confirms that existing guest images are preserved.
- Both real-runtime fault cases passed, with and without continuing renewal.
  Pause requests returned in about 0.6 ms; renewal could not extend the recovery
  deadline. Commands worked while the deliberately frozen executor remained
  frozen. This injects non-acknowledgement, not RabbitMQ's exact signal-loss race.
- Eight guest threads spinning on a userspace lock made progress under a
  0.5-CPU quota, through 43 throttle stops, while a peer kept answering commands.
- All five demand-sharing cases passed. The 36-sandbox scale test used 17,821
  host processes, about 0.21 broker CPU, and 42 ms median / 144 ms maximum command
  latency. Broker freeze/death and an unresponsive Sentry also passed recovery.
  These are measurements from this shared host, not general latency guarantees.

The first fault-test fixture incorrectly let the broker cancel its injected
pause; the corrected fixture briefly freezes only its disposable worker's
broker. A test also passed an unsupported timeout argument to `Process.result`;
that fixture was corrected. The final seven CPU failure/scale cases passed,
with the separate old-engine compatibility case skipped. Summaries are in
`cpu-pause-acceptance.json`; raw logs stay in the ignored scratch test directory.

## Earlier scaling implementation

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

# CPU demand sharing

The old broker divided available CPU time among active sandboxes by weight,
redistributing only around explicit quotas. An active sandbox could leave most
of its share unused while another sandbox was throttled. The existing tests
covered fully busy and fully idle peers, but missed partially active peers.

## Allocation

The broker now estimates demand from host CPU consumption during unthrottled
intervals. It collects about 100 ms of such intervals, preserving partial
observations across its own pauses. Paused time is not evidence of low demand.

Persistently runnable threads protect the share of work that is waiting for
host CPU time. Process leaders are observed on the normal 20 ms control tick;
their runnable state must persist for 40 ms to raise demand above consumption.
Other threads are inspected at observation boundaries, including threads whose
process leader sleeps. Brief wakeups do not reserve their peak concurrency
through subsequent idle intervals. An incomplete thread sample makes demand
unknown rather than silently treating work as idle. New descendants enter the
existing process-tree discovery cycle, normally every 250 ms.

Weighted allocation first satisfies demand-limited peers and redistributes the
remainder. If all peers appear to need less than capacity, the remaining time
stays available by weight so observations cannot become permanent ceilings on
new work. Both passes retain explicit quotas. A paused peer receives its normal
weighted entitlement while recovering, so a stale low estimate cannot prevent
it from waking to reveal new demand.

Small shares also need a burst allowance large enough for the host's CPU
accounting granularity: at least two clock ticks. Idle peers accrue bounded
credit at their ordinary weighted rate, capped by an explicit quota when set.
Idle transitions no longer erase credit or forgive CPU debt. This prevents a
rounded accounting tick from repeatedly pausing an intermittent peer and
making its demand unknown again.

The broker's private status includes `demand_cpus` and `rate_cpus` alongside
consumption, credit and pause counts. No public API argument or runtime binary
changes. Worker software identity includes the modified engine script; existing
workers and user sandboxes retain their original code.

## Scope

This remains a sampled userspace controller over one worker workspace and CPU
mask. It accounts runtime and transport CPU but pauses only guest execution.
Sampling delay, short bursts and external host contention still apply. It does
not reserve host cores, provide instantaneous kernel scheduling, or change
`vcpus`: guest CPU visibility and per-address-space execution concurrency remain
distinct from aggregate sandbox CPU time.

## Regression coverage

`tests/test_cpu_demand.py` covers partially active peers, 128-way allocation,
weight reclamation, explicit quotas, spare capacity for new work, interrupted
observation windows, short wakeups, sleeping process leaders, unreadable thread
samples, accounting-tick bursts and preserved quota debt. The two basic lending
cases fail against the unmodified 0.2.5 broker.

`tests/integration/test_cpu_demand_live.py` creates its own worker restricted to
four inherited CPUs and checks the staged broker source. It measures actual
host CPU counters as two sandboxes change demand at weights 1:1 and 3:1,
an explicit quota, one busy sandbox beside fifteen intermittent peers,
and user pause/resume. Each sandbox has `vcpus=2`; the busy workload uses twelve
separate guest processes. Reports retain per-sandbox usage, demand, assigned
rates, pauses, and host idle time. Fixture cleanup terminates only its own
sandboxes and stops its idle worker.

Run with a fresh artifact directory and an explicitly selected prepared coding
runtime. The invoking process should have four quiet CPUs available; the test
uses the first four CPUs in its inherited affinity mask.

```bash
SANDWEAVE_CPU_INTEGRATION=/tmp/sandweave-cpu-acceptance \
SANDWEAVE_ASSETS=/path/to/prepared-coding-runtime \
python -m pytest -q tests/integration/test_cpu_demand_live.py
```

## Live acceptance: 2026-09-11 UTC

The fixed broker passed all five live cases on four eligible host CPUs. Tests
ran sequentially using private workers; the host CPUs were not exclusive.
Both sandboxes in each pair advertised two virtual CPUs. Each measurement
covered eight seconds after settling. Values below are aggregate host CPU
seconds divided by elapsed time, including runtime and transport processes.

| Workload | First sandbox | Second sandbox | Host idle |
| --- | ---: | ---: | ---: |
| Previous broker: one busy process beside twelve, equal weights | 0.936 | 2.004 | 22.6% |
| Fixed broker: same workload | 0.636 | 2.975 | 5.7% |
| Both fully busy, equal weights | 1.875 | 1.932 | 0.0% |
| One busy process beside twelve, weights 3:1 | 0.567 | 2.979 | 7.1% |
| Both fully busy, weights 3:1 | 2.827 | 0.995 | 0.1% |
| Second sandbox limited to 1.5 CPUs | 0.954 | 1.501 | 34.0% |
| First sandbox explicitly paused | 0.000 | 3.805 | 0.0% |
| First sandbox resumed, weights 3:1 | 2.849 | 0.993 | 0.0% |

With sixteen equally weighted sandboxes, fifteen intermittent peers each used
0.074–0.082 CPUs and the busy sandbox used 2.194 CPUs. Host idle time was 6.9%.
This exercises borrowing while peers continue working, rather than relying on
them becoming fully idle. The old two-sandbox broker failed the same lending
assertion on the same CPU mask; its source came from commit `8de4113`.

Machine-readable reports are retained in the ignored directory
`runs/cpu-demand-sharing-20260911/{baseline,fixed}/`. They contain counters and
broker observations, without worker connection credentials. Each live fixture
confirmed termination of its own sandboxes before stopping its worker. No
existing user worker or sandbox was modified.

The unit suite passed with 284 tests, four skips and 98 integration tests
deselected. The three standalone broker allocation tests also passed. The
documentation check validated 22 pages and 77 Python examples.

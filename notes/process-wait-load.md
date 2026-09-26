# Process waits under worker load

Sandweave 0.2.31 replaces SDK completion and output polling with bounded waits
on guest notifications. No public method signatures or output limits change.

## Cause

The synchronous SDK polled `process_status` every 10 ms for the entire command.
Async waits capped their delay at 50 ms. Each request decoded the worker sandbox
record, then the runtime router decoded it again, before requesting guest status.
Silent stream readers made similar repeated output/status requests. Long hooks
therefore consumed worker CPU despite doing no useful host-side work.

## Change

The guest signals completion after persisting exit state and draining output.
Output readers wait on a condition signaled after bytes are flushed, or on exit.
The worker resolves the agent from one sandbox record. A `process_wait` request
releases the sandbox lifecycle lock before waiting, so stdin, other commands,
termination, and snapshot operations can proceed. Template setup uses the same
wait path. Service members support native async forwarding as well.

Each request waits at most ten seconds, returning immediately on notification.
A still-running command renews its wait without a fixed completion-latency penalty.
A local wait deadline clips that interval. Async cancellation stops the caller's
request and leaves the command alive; disconnected server waits expire within the
bounded interval. The existing HTTP servers still use connection-handler threads;
this change removes repeated work, not that architectural limit.

New workers advertise waiting support in process status. An older worker is
polled with exponential backoff from 10 to 250 ms, without trying unauthorized
new operations. A restored old guest agent is detected by its exact unsupported
operation response; the new worker polls it through its resolved connection with
the same backoff. Actual guest errors are propagated.

## Measurements

`tests/integration/test_process_wait_live.py` ran on babel-u9-24 in the existing
allocation, with affinity 16–23, no host sudo or KVM. Four disposable gVisor
guests hosted 128 concurrent real commands, and a fifth ran unrelated probes.
Both sync and async wait paths were exercised. Probe samples exclude initial
command startup. Scratch free space remained above 15% (about 21% free).

The baseline reproduces old client polling on the **updated worker**: 10 ms
sync polling, and 50 ms async polling. This isolates client polling cost; it
is not a full old-worker comparison or a replay of the client's application.
The baseline async delay omits its short initial ramp. Each measurement contains
12 samples per probe type; request counts cover the entire waiting phase,
including its one-second warmup and releasing command stdin.

| Wait path | Worker CPU, cores | Status requests | Blocking waits | Owner registration p50/max | List p50/max | Unrelated command p50/max |
| --- | ---: | ---: | ---: | --- | --- | --- |
| Sync polling baseline | 1.118 | 14,076 | 0 | 2.58/5.11 ms | 62.98/71.82 ms | 32.53/46.08 ms |
| Sync notifications | 0.044 | 128 | 128 | 1.07/1.32 ms | 8.11/9.13 ms | 17.95/22.04 ms |
| Async polling baseline | 1.074 | 8,969 | 0 | 2.68/4.61 ms | 72.17/94.67 ms | 30.18/56.16 ms |
| Async notifications | 0.061 | 128 | 128 | 1.11/3.63 ms | 10.29/13.04 ms | 18.14/25.20 ms |

The sync steady-state CPU reduction was approximately 25x. These measurements
establish the improvement for 128 waiting calls across four guests on this host;
they do not establish a maximum cluster size or production tail latency.
Machine-readable measurements are in `process-wait-load.json`.

Regression coverage includes notification races, complete final output, split
UTF-8 sequences, multiple waiters, stdin during a wait, local deadlines, legacy
worker/guest behavior, scoped authorization, service routing, and cancellation.
Live Weave tests exercise commands and pool leases through two outbound worker
bridges. Release acceptance additionally exercises the installed wheel's SDK,
command deadlines/output limits, filesystem and memory snapshots, and concurrent
waits. The new live regression is also part of the default deploy validation.

Upgrade clients and workers for notifications; existing guest agents and memory
snapshots remain compatible through backoff. The engine binary is unchanged.

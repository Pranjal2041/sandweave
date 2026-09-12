# Weave request latency

Sandweave 0.2.12 removes controller database reads and commits from forwarded
tool requests. It preserves the public sandbox and pool contracts.

## Causes and changes

The old controller repeatedly decoded retained allocation records while holding
one database lock. Owner renewal scanned every allocation; reconciliation also
walked completed allocations. Long-poll relay connections and forwarded requests
occupied threads. Worker observations competed with launches and termination.
The worker listener had a five-connection backlog, and synchronous guest
connections accumulated separately for each calling thread.

The controller now maintains indexed memory records. An ordered background
writer persists recoverable observations. Reservations, ownership changes and
job results become visible only after their transaction commits. SQLite remains
the persistence layer, with the existing on-disk schema and controller lock.
This does not introduce multiple scheduling authorities or asynchronous
acknowledgements of uncommitted lifecycle changes.

HTTP serving, forwarding and worker bridges use async I/O. Relay requests and
responses are batched when both peers support it. Older peers retain the single
message protocol. Dashboard work and worker observations use separate execution
queues. SDK command execution, process polling, streams and file reads/writes
have native async transports. Synchronous requests borrow exclusive reusable
connections instead of retaining one per calling thread. Worker listeners use
the system connection backlog.

Monitoring reuses unchanged metadata and completed-allocation rows. Pool status
and lease reconciliation select live records; preparation-failure checks read
only their required historical fields. Completed process status is cached, and
stream reads use 1 MiB chunks. Inline output and spool limits retain their
existing meanings.

## Measurements

These are measurements on one Linux host, not a claim of cross-region latency
or a distributed training benchmark. No user's running controller or sandboxes
were restarted. Raw databases and credentials remain private.

`scripts/profile-weave-control.py` uses a separate controller process, HTTP,
an outbound worker bridge, 500 active records and a simulated worker with a
1 ms response delay. Reconciliation, owner renewal and dashboard collection
remain enabled. Every response is checked against its request. The controller
comparison uses the archived 0.2.11 controller with the same benchmark client
and bridge; it isolates the controller changes rather than comparing two entire
SDK releases.

| Controller / workload | Requests/s | Request p50 | Request p95 | Peak controller threads |
| --- | ---: | ---: | ---: | ---: |
| 0.2.11; 64 callers; 10,000 retained records | 323 | 225 ms | 376 ms | 110 |
| New; 64 callers; 10,000 retained records | 1,890 | 28 ms | 49 ms | 22 |
| New; 1,000 callers; 10,000 retained records | 1,633 | 556 ms | 1,056 ms | 22 |
| New; 1,000 callers; 50,000 retained records | 1,198 | 580 ms | 1,663 ms | 22 |

The 64-caller runs lasted 15 seconds; the 1,000-caller runs lasted 30 seconds.
The latter completed 49,277 and 36,303 requests respectively. Peak controller
RSS was about 198, 218 and 727 MiB across the three new-controller runs.
At 50,000 retained records, p99 was 4.52 seconds, including the initial dashboard
history materialization. Higher retained history still has a collection and
memory cost; these results do not establish unlimited scale.

`tests/integration/test_weave_latency_live.py` used 16 real gVisor sandboxes
across two dedicated workers, each allowed two CPUs and 4 GiB. Each sandbox
requested 256 MiB guest memory and 256 MiB runtime memory. Both workers reached
the controller through outbound bridges. Warmup is excluded from action timings.

| Operation | Samples | p50 | p95 | Maximum |
| --- | ---: | ---: | ---: | ---: |
| File read, with 16 concurrent episodes | 128 | 5 ms | 22 ms | 103 ms |
| Full `env.run.aio("cat /payload")` | 128 | 111 ms | 228 ms | 349 ms |
| Guest execution within those commands | 128 | 20 ms | 81 ms | 164 ms |
| Completed-status RPC, 512-request simultaneous burst | 512 | 287 ms | 1,123 ms | 1,164 ms |

The complete 3 MiB stdout stream was also read and checked. There were no
command, file, status or output mismatches in the passing run. An earlier burst
failed before the worker backlog and connection-reuse fixes; it is not counted
as a passing performance result.

## Regression coverage

- A real HTTP listener admits 1,000 simultaneous forwarded requests while the
  persistence writer is deliberately blocked. Replies correlate correctly and
  requests do not create a thread each.
- Critical changes remain unpublished until their durable commit. Rollback,
  independent readers, backup failures, restart recovery and late relay replies
  are covered. Cached monitoring records refresh when terminal history changes.
- Live tests cover controller SIGKILL/restart, durable jobs, failures, retries,
  deadlines, snapshots, cross-worker transfer, pristine pools, CLI behavior,
  concurrent artifact imports and 16 simultaneous leases.
- Real browser checks cover dashboard login, navigation, filters, live updates,
  history, logs, logout and mobile layout.

Upgrade the SDK in clients, controllers and worker processes to apply all of
these changes. Existing running Python processes keep their loaded code.
The separately observed serialization in an application's trace writer is
outside this SDK change.

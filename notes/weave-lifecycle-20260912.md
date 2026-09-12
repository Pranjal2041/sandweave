# Pool lifecycle concurrency

Sandweave 0.2.18 removes worker reply waits from the controller's lifecycle
executor. The public API, worker protocol and persisted record format stay the
same. Upgrade and restart the controller to apply the change.

## Cause and implementation

Forwarded tool calls already used async sockets, but creates, claims,
termination and related lifecycle operations synchronously waited for those
sockets from a shared 16-thread executor. Hundreds of acquisitions therefore
queued behind the first 16 worker replies. Separately, lease polling entered a
write transaction, and pool status cloned full allocation definitions before
discarding almost all of their fields.

Lifecycle functions now yield worker requests between state transitions. The
controller waits through the existing async HTTP or relay transport; bounded
threads execute the state transitions and their durable commits. A transaction
never spans an async wait. Each allocation keeps one future until the complete
operation finishes, including its final commit. Observations share that same
allocation key. Shutdown interrupts requests, drains their state transitions,
then closes executors, transports and persistence. Direct internal calls retain
their synchronous behavior.

Pool status and lease reads use indexed, projected memory records outside the
control executor and writer lock. A brief memory read section keeps a lease and
its route consistent with the published transaction. Failed-pool polling reports
the failure without mutating records; reconciliation persists failure and
cleanup. Claim validation also reads only its required fields without taking
the writer lock.

The shared-image preparation futures, transfer executor, cache identities,
integrity checks, assignment generations and uncertain-outcome handling remain
in place. Cache-identity RPC waits also release lifecycle threads.

## Controller measurements

`scripts/profile-weave-lifecycle.py` compares the published 0.2.17 package with
the candidate using the same 256 durable claims, a real HTTP worker listener,
a simulated one-second worker delay and 64 concurrent lease pollers. It starts
at the claim boundary; these are controller measurements, not guest startup
times. Neither run restarts a user's controller or launches a sandbox.

| Measurement | 0.2.17 | 0.2.18 |
| --- | ---: | ---: |
| Simultaneous worker requests | 16 | 256 |
| Complete 256-claim burst | 16.53 s | 1.82 s |
| Unrelated executor work waiting in the queue | 16.15 s | 1.7 ms |
| Lease-poll median | 12.1 ms | 5.0 ms |
| Lease-poll p99 | 27.4 ms | 130.3 ms |

Polling continues until all claims complete, so these runs cover different time
windows and sample counts. The shorter burst has a higher polling tail; this
is not a claim that every latency percentile improved. Full results, including
p95, maximum and sample counts, are in `weave-lifecycle-20260912.json`.

The concurrency regressions hold all 256 replies until every request has
arrived, separately for creation, claim and termination over HTTP and relay.
They check that unrelated create/terminate work can complete while those replies
remain blocked, that stale responses cannot revive cancelled allocations, and
that another operation cannot overlap the allocation's existing future.
Additional checks cover shutdown during all three lifecycle phases, recovery of
unresolved reservations, failed-pool reads and cross-pool lease access.

The status regression blocks persistence, holds the transaction writer lock,
and occupies every control-server thread. All 256 lease polls and a pool status
request still complete. It also rejects accidental cloning of setup payloads.
Existing image-deduplication, worker-loss and immutable-publication tests remain
part of the host suite.

## Runtime validation

The live tests use two disposable gVisor workers reached through outbound relay,
with no GPUs. They cover public Pool acquisition, pristine replacement, commands,
files, controller restart during a job, retries, shared-cache restoration,
retention cleanup and preservation of an unrelated sandbox and saved cache.
The latency test acquires 16 real sandboxes concurrently and checks 128 commands,
128 file reads, 512 status requests and a complete 3 MiB output stream.

The host suite passed 437 tests, with two unrelated optional tests skipped.
All 13 live checks passed in 145.63 seconds on the final runtime changes. The
16 warm-pool acquisitions had a 224 ms median, 233 ms p95 and 260 ms maximum;
warmup is excluded. Complete command calls had a 134 ms median and 201 ms p95.
The full live latency report accompanies the controller measurements in the
JSON record. Local logs and JUnit receipts are retained under
`runs/weave-lifecycle-20260912/`.
The gVisor engine is unchanged at
`dd5239ad8db0753e8af7da0ea915df71c5357f7c`.

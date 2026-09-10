# Weave design evidence and engineering details

Research date: 2026-09-09. This supports the [proposed design](weave-design.md).
Source inspection and upstream documentation are evidence about design and
existing code, not successful runtime tests of the proposed system.

## Existing Sandweave foundation

| Source | Finding and consequence |
| --- | --- |
| [Pool](../src/sandweave/sandbox/pool.py) | Coordination lives in the client process, with round-robin target selection. It pins a baseline, replaces used guests and refills an idle reserve. Distributed durability requires moving coordination into Weave while retaining those semantics. |
| [Worker](../src/sandweave/sandbox/worker.py) | Local records, operation identities, resource admission and cleanup already exist. Reuse this executor; add versioned assignments and observed-state reporting rather than another execution engine. |
| [Resources](../src/sandweave/sandbox/resources.py) and [admission](../src/sandweave/sandbox/admission.py) | Guest and runtime memory both consume the budget. Virtual CPUs are shared, not physical-core reservations. Cluster scheduling must not reinterpret `cpu=4` as four dedicated cores. |
| [Ownership](../src/sandweave/sandbox/ownership.py) and [connection](../src/sandweave/sandbox/connection.py) | Owner heartbeats and expiry tombstones exist; lost mutation replies can have uncertain outcomes. Preserve these distinctions across controller restarts. The existing worker token is not a complete multi-tenant authorization scheme. |
| [Snapshots](../src/sandweave/sandbox/snapshots.py) | References and materialization currently depend on accessible paths. Cross-worker recovery needs durable artifact publication and compatibility checks, not merely passing a snapshot ID. |
| [Targets](../src/sandweave/sandbox/targets.py) | Local, SSH and Slurm paths already exist. Separate capacity acquisition from connection setup. Retain the distinction between owned jobs and borrowed allocations. |

## Designs consulted

| Primary source | Relevant mechanism | Choice for Weave |
| --- | --- | --- |
| [Kubernetes controllers](https://kubernetes.io/docs/concepts/architecture/controller/) | Reconcile observed resources with persistent desired state. | Use one durable model and repeatable reconciliation, rather than making client scripts responsible for cluster repair. |
| [Nomad scheduling](https://developer.hashicorp.com/nomad/docs/concepts/scheduling/how-scheduling-works) | Evaluate demand, filter/rank placements and commit an allocation plan. | Keep scheduling a testable planning component; commit reservations before asking workers to execute. |
| [Kueue cluster queues](https://kueue.sigs.k8s.io/docs/concepts/cluster_queue/) and [admission fairness](https://kueue.sigs.k8s.io/docs/concepts/admission_fair_sharing/) | Quota sharing and historical usage affect admission; immediate admission accounting prevents bursts from outrunning usage updates. | Separate quota admission from placement, while reserving both atomically; charge starting and warm allocations immediately. |
| [KAI Scheduler](https://github.com/kai-scheduler/KAI-Scheduler/blob/4fd02b1e27c8aa12cec642b8320ad6b3585f6d94/README.md) | Hierarchical queues, resource fairness, grouped scheduling and separate priority/preemption settings. | Support heterogeneous workloads and explicit interruption policies. Reuse ideas without requiring its Kubernetes deployment. |
| [Agent Sandbox](https://github.com/kubernetes-sigs/agent-sandbox/blob/d9a67516d0cf74359455a605d84eb27306de6d1e/README.md) | Sandbox templates, warm pools and exclusive claims. | Preserve the existing Pool API, with durable exclusive ownership of ready members. |
| [OpenSandbox pool design](https://github.com/opensandbox-group/OpenSandbox/blob/40d72b7c64ebcdda5f7fe06f87829b553579432d/docs/kubernetes/index.md) | Ready buffers, batch delivery, pool shape constraints and graceful eviction. | Match the full prepared sandbox specification; expose busy, ready, starting and waiting counts separately. |
| [Ray actor fault tolerance](https://docs.ray.io/en/latest/ray-core/fault_tolerance/actors.html) | Process reconstruction does not automatically restore application state; retried calls can repeat execution. | Make recovery policy and attempt identity explicit. Report uncertain execution rather than concealing it with retries. |
| [Prime RL architecture](https://github.com/PrimeIntellect-ai/prime-rl/blob/aa3a6522b4743d25c46af185fb497c279e668dfb/docs/overview.md) | Environment orchestration, inference and training have different responsibilities. | Manage environment capacity and failures; leave learning algorithms and policy freshness with the training framework. |

Three implementation reads sharpened those choices:

- Agent Sandbox's [write-behind helper](https://github.com/kubernetes-sigs/agent-sandbox/blob/d9a67516d0cf74359455a605d84eb27306de6d1e/controllers/writebehind_requeue.go)
  defers recomputable updates. This supports coalescing observations, not delaying
  the durable ownership record needed for a safe handoff.
- OpenSandbox's [task recovery code](https://github.com/opensandbox-group/OpenSandbox/blob/40d72b7c64ebcdda5f7fe06f87829b553579432d/kubernetes/internal/scheduler/recovery.go)
  documents an ambiguity between an unstarted and completed task during recovery.
  Durable attempt records and acknowledged result commits must be first-class
  parts of Weave's protocol.
- Prime RL's [concurrency controller](https://github.com/PrimeIntellect-ai/prime-rl/blob/aa3a6522b4743d25c46af185fb497c279e668dfb/src/prime_rl/orchestrator/concurrency.py)
  uses pressure signals, cooldowns and freshness checks. Work admission, sandbox
  refill and machine provisioning need different response times, with a common
  demand limit so they do not amplify each other's scaling decisions.

These are architectural precedents, not evidence that Weave already has their
features or performance. The pinned source URLs identify the repository revisions
consulted; documentation outside those repositories can change.

## Scheduling details

Quota determines what a project may consume. Weight influences its share under
contention. Priority expresses urgency within the authorized policy. Preemptibility
determines whether already running work may be interrupted. None substitutes for
the others, and no weight promises a fixed ratio of completed episodes.

Use weighted dominant-resource allocation as the initial fairness policy, with
aging and optional usage history to account for long-lived work. The
[DRF paper](https://www.usenix.org/conference/nsdi11/dominant-resource-fairness-fair-allocation-multiple-resource-types)
provides the multi-resource basis. Heterogeneous devices, placement constraints
and non-preemptible episodes limit attainable shares; report the actual allocation
and the constraints rather than promising textbook guarantees.

CPU admission needs an explicit reservation policy separate from vCPU count,
quota and runtime CPU weight. Memory includes guest, runtime and worker overhead.
GPU inventory identifies device models and UUIDs; sharing is accepted only where
the selected runtime supports the requested enforcement. Labels express topology
and placement constraints, not proof of isolation. Pending and stopping resources
remain charged until ownership is resolved or termination confirmed.

Shared-CPU pools use configured worker concurrency/oversubscription ceilings and
observed pressure. Dedicated CPU reservations require explicit support and an
explicit request. Report which limits are enforced; admission accounting alone
cannot reserve physical resources against unrelated processes on a shared host.

For grouped starts, reserve every member, prepare them, then release a start
barrier. A preparation deadline aborts the group and releases confirmed-unused
reservations. This is coordinated admission, not a guarantee against a member
failing immediately after the barrier. Backfilling and queue aging must not
permanently starve groups that need more machines.

The controller scores only feasible workers. Prefer a suitable warm guest, then
a cached baseline, then cold preparation, subject to fairness and explicit
affinity/spread policies. Moving active state is a separate, potentially
disruptive operation; balancing should normally place new work.

## Warm state, storage and recovery

A warm match includes immutable template/setup inputs, runtime revision, resource
shape, network rules, mounts and project ownership. A resource override either
finds a matching pool member or starts the correct shape; it cannot silently
relabel a running guest. Inject claim-specific secrets after ownership is assigned.
Released guests are destroyed; their modified state never becomes the next user's
baseline implicitly. Saved shared baselines need explicit publication and access
control.

Publish a checkpoint's data durably before publishing its recoverable reference.
An episode manifest pairs that reference with application-owned agent state at
an agreed step boundary. Restore validates engine/image versions, CPU requirements,
resource bindings, mounts and external dependencies. External services and mutable
volumes are not rolled back simply because guest files were restored.

Pausing retains resources unless a supported checkpoint-and-stop path explicitly
releases them. Planned draining allows checkpointing and episode completion;
unexpected host loss can recover only a previously durable checkpoint. Kubernetes
likewise distinguishes [planned and involuntary disruptions](https://kubernetes.io/docs/concepts/workloads/pods/disruptions/);
disruption budgets do not prevent physical failures.

## Failure and control-state details

Track desired generation, worker incarnation, sandbox attempt and operation ID
separately. A stable sandbox ID identifies the logical resource; a new attempt
does not inherit an old attempt's authority. Termination leaves a durable tombstone
until retries and retained references can no longer recreate it.

For a missing heartbeat, first mark the worker unreachable and stop placing new
work there. Existing callers may still reach it. A timeout cannot establish that
the old guest has stopped. Stateful failover requires a confirmed stop, provider
fencing, or storage/service tokens that reject writes from superseded attempts.
Otherwise pause recovery or use an explicitly replay-safe policy.

During controller outages, active worker sessions can continue within their
existing authority. Client-owner heartbeats must not depend solely on a healthy
controller. A rejoining controller reconciles worker journals before reclaiming
uncertain allocations. It must neither delete live sessions merely because it
restarted nor hand the same warm guest to another caller.

Workers can eventually receive bounded resource grants for local, batched claims.
Each claim needs a durable journal and a stable retry destination; an ambiguous
claim cannot be retried freely at another worker. Expiring a grant prevents new
claims but does not prove its old guests stopped. Begin with central atomic
reservations and add delegation only after measuring the bottleneck.

Use indexed inventories, incremental reservation counters and queues of changed
objects. Reconciliation is event-driven with periodic repair sweeps. Batch worker
reports and launch requests, and apply backpressure before unbounded queues form.
If scheduling later needs partitioning, assign each capacity partition a fenced
owner and preserve atomic project quotas across partitions; adding scheduler
replicas must not multiply their allocation authority.

SQLite is suitable for the single-controller mode on durable local storage;
[WAL does not support network filesystems](https://www.sqlite.org/wal.html).
PostgreSQL [transactional locking](https://www.postgresql.org/docs/current/explicit-locking.html)
can arbitrate reservations in the HA mode. Database deployment, backups and
failover are explicit dependencies; multiple controller processes alone do not
provide high availability. Versioned watches need snapshots and replay cursors
so reconnecting clients can recover after event retention expires.

Provider operations use stable request keys and reconcile inventory after lost
responses. Only release externally confirmed resources bearing Weave's ownership
identity. Providers must identify their actual privileges: SSH can attach to an
existing machine, Slurm can request an allocation, and a configured cloud account
can create machines. None makes an incompatible host suitable for the runtime.

## Acceptance before release

The proposed system needs fault injection as well as throughput measurements:

- Lose the controller after reservation, the worker after launch, and the reply
  after a successful claim. Verify reconciliation, accounting and exclusive claims.
- Partition and rejoin workers and controllers. Reject stale attempts; demonstrate
  that active episodes are not silently duplicated or replaced.
- Lose a host and its local disk; restore only published compatible checkpoints.
  Exercise unsupported GPU state, missing artifacts and external-state conflicts.
- Exhaust memory, fragment GPU capacity, register overlapping allocations and
  overload CPU shares. Explain waiting without overselling resource guarantees.
- Mix short coding tasks, long desktop/VR episodes and grouped GPU work. Measure
  shares, starvation, queue time and preemption effects, including warm capacity.
- Lose provider creation responses and simulate widespread node failures. Bound
  retries, concurrent provisioning and cost; preserve borrowed allocations.
- Upgrade or drain during active episodes. Respect pinned versions and planned
  interruption limits; cancel safely when a checkpoint fails.
- Crash submitting Python processes, reconnect borrowed handles and restart the
  controller. Verify attached cleanup, detached persistence and TTL behavior.
- Submit duplicate results, infrastructure failures and stale policy metadata.
  Preserve episode identities and distinguish failures from rewards.
- Exercise cross-project secrets, caches, volumes, endpoints and worker RPCs.
  Prove authorization and advertised isolation, including the forwarding path.
- Measure claim/action latency and controller load at increasing worker counts,
  with cold caches, contention, bursts and slow artifact storage. Include p95/p99,
  not only warm single-user averages, and test restoration from metadata backups.

No new sandbox or workload was launched for this design task.

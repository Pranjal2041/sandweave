# Weave: sandbox management

Status: the initial coordination stage is implemented. The [usage guide](weave-usage.md)
describes the available API; the broader design below remains the roadmap.
[Research and engineering details](weave-research.md) support these choices.

Implemented: existing-worker registration, atomic reservations, assignment
generations, direct sandbox access, weighted placement, ready pools, policy
updates, draining, verified snapshot transfer, durable commands/batches/repeats,
explicit retries, ownership, events, backups and controller restart recovery.

Remaining: automatic machine provisioning and autoscaling, gang admission,
project quotas and borrowing, preemption, rollout policies, service routing,
team permissions, secret distribution, garbage collection, controller failover
and large-cluster performance qualification. Existing `Slurm.acquire` can still
request an allocation explicitly; Weave does not yet scale machine providers.

**Templates define environments. Sandbox code runs one environment. Weave keeps
the requested environments available across workers.** A controller records what
users want, compares it with worker reports, and repairs differences. Workers
execute locally. Providers acquire and release machines or allocations.

## Repository structure

```text
src/sandweave/
├── templates/             # Define and prepare environments.
├── sandbox/               # Run and control one environment on a worker.
└── weave/                 # Manage environments across workers.
    ├── pool.py
    ├── jobs.py
    ├── controller.py
    ├── scheduler.py
    ├── state.py
    └── providers/
```

These are three code responsibilities, not three services. The smallest cluster
runs one controller alongside an existing worker. Pool coordination moves into
`weave`; public imports remain unchanged. Runtime execution stays in the existing
worker. Machine acquisition moves out of transport code into providers. A cluster
target registers at the target connection boundary; execution code does not
import controller or cloud-provider implementations.

## User contract

Existing local code keeps working. A named cluster becomes another target:

```python
from sandweave import Sandbox, Pool

with Sandbox(template="gnome", target="lab", cpu=4, memory="8GiB") as env:
    image = env.desktop.screenshot()

with Pool(template="coding", target="lab", size=128, warm=16, weight=2) as pool:
    with pool.acquire() as env:
        result = env.run("python -c 'print(2 + 2)'")
```

`lab` is a configured cluster target. `size` remains the total sandbox ceiling;
`warm` is the desired idle, ready reserve within that ceiling. Quotas and available
machines may prevent either target from being met; status explains why. `weight`
is a pool scheduling option, separate from a sandbox's CPU weight.

A **Pool** manages capacity. A **Job** manages submitted work: a command, immutable
program and inputs, and a completion/retry policy. Jobs can run once, as batches,
on a schedule, continuously, or once per eligible worker. They use the same
sandbox lifecycle. `Pool.map` continues to execute caller-side Python callbacks;
durable execution requires an explicitly submitted program.

Ownership stays explicit: attached resources stop when their creating process
exits; `detached=True` permits them to outlive it. TTLs and explicit termination
still apply. Attaching a new handle does not transfer ownership. A controller
restart must not be mistaken for the creator exiting.

The CLI adds `sandweave cluster` commands for starting, joining and inspecting a
cluster, plus worker draining and pool/job inspection. `setup` and `doctor` retain
their existing roles, including interactive configuration and repairs. Creating
additional machines requires a configured provider and explicit capacity/budget
limits; choosing a cluster target alone does not authorize unlimited provisioning.

## Management features

| Area | Proposed behavior |
| --- | --- |
| Lifecycle | Persist desired state; reconcile creation, health, restart, expiry and deletion. Distinguish startup, readiness, application failure and worker failure. Use bounded retries and backoff. |
| Placement | Filter by eligible CPU capacity, total memory, GPU model, runtime compatibility, storage and network requirements; then choose packing, spreading or affinity. Reserve groups together when they must start together. |
| Shared capacity | Project quotas and borrowing, weighted resource shares, priority, queue aging and explicit preemption policy. Account for warm and starting sandboxes as well as busy ones. |
| Preloading | Prepare runtime files, images and pinned baselines on eligible workers. Maintain pristine, ready sandboxes; claim one exclusively and refill after use. Limit concurrent downloads and preparation. |
| Scaling | Scale sandbox capacity from queue age, demand and readiness time. Acquire workers separately through Slurm, cloud or other providers, with limits, cooldowns and scale-down draining. Existing SSH machines supply fixed capacity. |
| Updates | Pin versions per active session. Roll out replacements, canary changes and roll back at release or episode boundaries. Respect limits on planned interruptions. |
| Work execution | Durable job status, completion counts, cancellation, deadlines, dependencies, retries and grouped admission. Keep service restart policy separate from finite job completion. |
| Saved state | Distribute immutable artifacts through shared or object storage. Support declared volume access rules and compatible snapshot restore. Local-only state cannot survive loss of its host. |
| Connectivity | Discover workers and declared services; balance stateless services. Keep each desktop or game session attached to its assigned sandbox. Offer authenticated forwarding when clients cannot reach workers directly. |
| Teams | Project-scoped authorization, quotas, secrets, configuration revisions, cache/volume access controls and audit records. Verify runtime enforcement before accepting an isolation requirement. |
| Operations | Explain waiting and failure states; expose events, logs, metrics and traces. Provide draining, backups, retention, garbage collection and controller failover. Version the API and worker protocol. |

This covers the major Kubernetes management categories. It does not imply
Kubernetes API compatibility or support for every networking and storage plugin.
Kubernetes can itself supply worker Pods through a provider, subject to runtime
qualification inside those Pods. It is not required to run Weave.

## What matters for RL and evaluations

**Recovering capacity and resuming an episode are different operations.** Empty
pool members can be replaced automatically. Losing an active sandbox reports an
interruption. A declared job or application policy decides whether to restart the
episode, restore it, or fail it; an existing handle must not silently receive a
fresh environment.

Episode recovery requires a saved sandbox reference and the agent's corresponding
conversation, random state, step and policy version. A filesystem snapshot
restarts processes; it does not restore their memory. Memory restore requires
compatible workers. Unsupported GPU or graphics state must be reported rather
than silently discarded.

Infrastructure failure is recorded separately from an agent reward. Attempts keep
their example and episode identities so retries do not silently change the
dataset. The training framework owns sampling, rewards, inference and policy
staleness. It can provide demand and concurrency limits to Weave; faster episode
completion alone must not redefine the requested workload mixture.

## Small contracts, explicit authority

| Component | Contract |
| --- | --- |
| State | Store versioned desired state, reservations, ownership and attempt results atomically. Support conditional updates and resumable watches. |
| Scheduler | Turn queued demand and eligible inventory into a placement plan, or a reason it cannot fit. It does not launch processes. |
| Controller | Commit reservations, issue repeatable operations and reconcile their outcomes. |
| Worker | Apply an assignment identified by operation, attempt and generation; enforce local admission; report observed state. |
| Provider | Ensure, discover and release owned capacity using stable request identities. Borrowed machines and allocations remain externally owned. |

A worker is an eligible resource allocation, which may be a whole machine or
part of one. Overlapping registrations must not count the same CPU/GPU capacity
twice. Host boot identity and worker incarnation distinguish a restarted process
from its predecessor.

There are three non-negotiable rules:

1. Reserve before starting and durably record ownership before handing a sandbox
   to a client. Retries reuse operation identities. Late commands and results
   from older attempts cannot overwrite the current attempt.
2. An unreachable worker is not a confirmed dead worker. Before replacing work
   that can write shared state, confirm the old instance stopped or revoke its
   ability to write. Otherwise retain an uncertain state, unless the workload
   explicitly permits duplicate execution. Lease expiry alone is insufficient.
3. Commit a result once, but do not promise a command executed once. Lost replies
   and retries can repeat external effects. Applications need appropriate
   idempotency or fenced destinations.

## Performance and deployment

Commands, observations and video can use the existing direct worker path when
the client can reach it. HTTP/HTTPS clients and outbound worker agents can instead
forward sandbox RPCs through the controller; payloads do not enter its database.
This permits clients and workers with no direct route to one another, at the cost
of an extra network hop. Arbitrary guest TCP ports remain separate.
Warm acquisition avoids preparation.
Bounded worker reservations can later support batched, local claims without
weakening ownership rules. Recoverable status updates can be coalesced; ownership
and result commits cannot.

Reconcile changed objects through indexed work queues. Maintain incremental
resource accounting instead of scanning every sandbox for every placement.

Start with one controller and durable SQLite storage. This mode has no
automatic controller-host failover. The implementation uses rollback journaling
and an exclusive controller lock; local storage and one NFS4 deployment have
passed restart checks. Prefer durable local storage; other filesystem/locking
configurations need deployment qualification.
High availability uses replicated controller processes and an independently
available PostgreSQL deployment, with fenced scheduling leadership and atomic
reservations. Artifact storage remains separate from metadata storage.

Measure queueing, preparation, claim, boot, restore and action latency separately,
including tail latency under contention. A few milliseconds is a warm local
acquisition target, not a cold-start or cross-network guarantee.

## Build order

1. Add durable coordination over existing workers: authenticated registration,
   ownership, admission, operation identity, reconciliation and failure handling.
   Prove recovery across two workers before adding automatic machine creation.
2. Add weighted scheduling, ready pools, artifact distribution, providers,
   autoscaling, draining and durable jobs with episode recovery hooks.
3. Complete controller high availability, team administration, rollout policies
   and operational tooling; qualify scale and failure behavior. Authentication
   and authorization for the initial deployment are required from the first step.

The [acceptance scenarios](weave-research.md#acceptance-before-release) define
the broader system's release requirements. [Implementation acceptance](weave-acceptance.md)
records the checks for the initial coordination stage.

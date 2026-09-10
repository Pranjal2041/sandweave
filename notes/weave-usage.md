# Manage sandboxes with Weave

Weave runs a controller alongside your Sandweave workers. The controller assigns
sandboxes, maintains pools and tracks submitted jobs. Commands and observations
travel directly between your Python process and the assigned worker.

This API is available in the source checkout. Install it with
`uv pip install -e .` before trying these examples; it is not in PyPI 0.1.2.

## Start a cluster

```bash
sandweave cluster start lab
sandweave cluster status lab
```

This starts a controller and registers a local worker. Controller state lives in
your configured Sandweave directory. Use `--directory /path/to/controller-state`
to choose a separate controller directory, and `--slots 8 --memory 16GiB` to limit
the capacity offered by its initial worker.

Use the cluster name wherever the SDK accepts a target:

```python
from sandweave import Sandbox

with Sandbox(target="lab") as env:
    result = env.run("python -c 'print(2 + 2)'")
    print(result.stdout)
```

Attached sandboxes still stop when their creating Python process exits.
`detached=True` keeps a sandbox running after that process exits. Creating a
sandbox waits for readiness; insufficient capacity appears as a waiting reason
in cluster status.

## Add workers

Register another machine through your existing SSH configuration:

```bash
sandweave cluster add lab --target ssh://worker-two --slots 8 --memory 16GiB
sandweave cluster workers lab
```

The worker must have the Sandweave Python package installed. SSH access uses your
normal account configuration. A selected worker prepares missing template files
on first use. Existing sandboxes keep their original runtime files. Workers retain the runtime's Linux and permission
requirements; Weave adds no requirement for host sudo or KVM.

If your client is on another machine, save the controller target there:

```bash
sandweave cluster connect lab --host controller-host --directory /path/to/controller-state
```

You can also register a worker from its own machine with `sandweave cluster join
lab`, after configuring that cluster target.

An existing Slurm allocation can supply a worker:

```python
from sandweave import Cluster, Slurm

cluster = Cluster.connect("lab")
worker = cluster.add_worker(Slurm.connect("12345"), slots=8, memory="16GiB")
```

Replace `12345` with your allocation ID. Registration borrows that allocation;
removing the worker does not cancel the job. Workers with overlapping CPU or GPU
allocations are rejected to avoid counting the same capacity twice.

## Maintain ready sandboxes

```python
from sandweave import Pool

with Pool(target="lab", template="coding", size=16, warm=4, weight=2) as pool:
    with pool.acquire() as env:
        result = env.run("python --version")
    print(pool.info)
    pool.update(size=32, warm=8, weight=3)
```

`size` is the total capacity ceiling, including idle sandboxes. `warm` is the
desired idle reserve within that ceiling. The controller prepares a baseline,
discards each used sandbox, and starts replacements from the pinned baseline.
Snapshots can move between workers with separate storage directories.

`weight` controls the pool's share when multiple pools are waiting. `priority`
controls which priority group is admitted first. `placement="spread"` distributes
new work; `placement="pack"` prefers workers already in use. These settings do
not interrupt an active episode.

The `update` call changes capacity without changing the template. Shrinking a pool retires idle members and
lets active leases finish. Worker slots bound concurrent sandboxes; advertised
guest vCPUs remain shared CPU settings, rather than dedicated-core reservations.
Both guest and runtime memory count toward admission.
GPU placement reserves a specific matching device. This initial scheduler counts
one whole GPU per sandbox, including sandboxes configured with MPS settings.
It does not yet admit multiple GPU shares onto the same device.

For a pool that survives the creating process:

```python
pool = Pool(target="lab", name="coding", size=16, warm=4, detached=True)
pool.start()

# In another Python process:
pool = Pool.connect("coding", target="lab")
with pool.acquire() as env:
    print(env.run("uname -s").stdout)
pool.close()
```

Connecting borrows a handle. Closing a borrowed handle leaves the pool running;
`pool.terminate()` closes the pool itself. A context that creates a pool closes
that pool on exit, including a detached pool. Each acquired lease belongs to its
acquiring process.

The existing sync and async `pool.map` APIs work with cluster pools. Their Python
callbacks run in the caller's process. Use a job for a submitted program that
must run independently of that process.

## Submit a job

```python
from sandweave import Job

job = Job.submit(
    "python /workspace/evaluate.py",
    files={"evaluate.py": "./evaluate.py"},
    target="lab",
    detached=True,
)
print(job.id)
result = job.result()
print(result.stdout, result.stderr, result.returncode)
```

Submission stores the program bytes and command before returning. A job can wait
for capacity without keeping the submitting Python process alive when detached.
Reconnect with `Job.connect(job_id, target="lab")`.

To submit to an existing pool, use `pool.submit(command, ...)`. To run a batch,
pass `items=[...]`; each command receives its JSON input in `SANDWEAVE_ITEM`, a
stable `SANDWEAVE_TASK_ID`, and `SANDWEAVE_ATTEMPT`. Batch results preserve input
order. `every=60` schedules another attempt 60 seconds after the previous attempt
finishes, until the job is cancelled.

Retries are explicit. `retries=2, retry_codes=[75]` permits two retries for that
exit code. `retry_infrastructure=True` additionally permits retries after a
confirmed infrastructure failure. A command can have external effects before
failing, so retryable programs must account for repeated execution. Unreachable
workers retain their uncertain attempts and resource reservations.

`job.wait(timeout=30)` limits how long the client waits. It does not cancel the
job. Pass `timeout=30` to submission to set the command's execution deadline.
Use `job.cancel()` to cancel it explicitly.
Nonzero exit codes return ordinary command results. `check=True` raises for those
codes; execution timeouts and output limits raise with the partial result.
Durable jobs retain at most 4 MiB of command output by default, configurable up to
16 MiB with `max_output_bytes`. Keep larger outputs in files or external storage.

## Inspect and drain

```python
from sandweave import Cluster

cluster = Cluster.connect("lab")
print(cluster.workers)
print(cluster.info)
print(cluster.events())

worker_id = cluster.workers[0]["id"]
cluster.drain(worker_id)
```

Draining stops new placement and retires idle pool members. Active leases finish
normally. `cluster.resume(worker_id)` makes the worker eligible again;
`cluster.remove_worker(worker_id)` unregisters it after its reservations have
been released.

```bash
sandweave pool create --target lab --name coding --size 16 --warm 4
sandweave pool status coding --target lab
sandweave pool exec coding --target lab -- "python --version"
sandweave pool update coding --target lab --size 32 --warm 8
sandweave pool close coding --target lab

sandweave job submit --target lab -- "python -c 'print(2 + 2)'"
sandweave job status JOB_ID --target lab
sandweave job result JOB_ID --target lab
```

## Controller lifetime

`cluster.stop()` stops the controller while retaining its database and existing
worker sandboxes. Start the same cluster again to resume coordination. Keep the
controller's state directory: it contains ownership, placement, pool and job
records. `cluster.backup(path)` saves a consistent database copy. Credentials and
process-owner leases are separate files, so preserve the controller directory
alongside that database backup.

This implementation uses one controller with SQLite and an exclusive controller
lock. Rollback journaling avoids WAL's shared-memory requirement. It supports
controller restart; automatic failover to another controller host is a separate
deployment feature. Backups of controller metadata do not contain sandbox disks
or external volumes.
Use durable local storage for controller state when available. Crash/restart
checks passed on local storage and on this cluster's NFS4 mount, with one
controller. This does not qualify every network filesystem or locking setup.
Events, completed jobs and saved artifacts currently remain until removed by the
operator. Automatic retention and garbage collection are not implemented.

The cluster currently assumes one trusted account. Its controller credential can
manage the cluster; sandbox routes carry credentials limited to one sandbox.
Team roles, automatic machine provisioning and the remaining deployment features
are tracked separately in the [design](weave-design.md).

See [acceptance results](weave-acceptance.md) for the tested deployment and scope.

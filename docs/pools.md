# Pools and evaluation

A pool bounds concurrency and can keep a reserve of sandboxes ready for work:

```python
from sandweave import Pool

def evaluate(env, source):
    env.files.write_text("/workspace/main.py", source)
    return env.run("python /workspace/main.py", timeout=5)

programs = ["print(1 + 1)", "print(2 + 2)"]

with Pool(template="coding", size=8, warm=2) as pool:
    results = list(pool.map(evaluate, programs))

for result in results:
    print(result.stdout, result.returncode)
```

The callback runs in your Python process. Results preserve input order. Each
task receives independent starting state; the pool discards used sandboxes and
replaces them from its baseline.

## Size and warm reserve

`size` limits concurrent leases. `warm` requests ready, idle environments within
that capacity. Entering the pool waits for its initial reserve. Actual readiness
is limited by worker resources.

If cluster pool preparation fails, pending and unissued leases receive the
preparation error. Issued leases remain usable until released or the pool closes.
When an error leaves a pool context, the SDK requests closure and propagates the
original error; the controller continues cleanup.

If a checkout reply is lost, the client still requests release using the same
lease ID. Uncertain release requests are retried within `wait_timeout`.

```python
with Pool(target="lab", size=8, warm=2) as pool:
    with pool.acquire() as env:
        print(env.run("python --version").stdout)
    pool.update(size=16, warm=4)
```

`target="lab"` uses [Weave](clusters.md) to place work across registered workers.
A printed cluster address works too. A local pool needs no controller setup.

## Distribute proxies

To distribute proxies across a pool, pass
`network=Network(proxy=proxies, policy=ProxyPolicy("same_region"))`.
The pool chooses a region once and cycles through its proxies across workers.
See [proxy policies](networking.md#proxy-policies) for the catalog format,
other distributions and standalone sandbox behavior.

## Prepare a baseline

```python
from sandweave import Pool, Sandbox

with Sandbox(setup="./install-tools.sh") as builder:
    baseline = builder.cache("evaluation-tools")

with Pool(cache=baseline, size=8, warm=2) as pool:
    results = list(pool.map(evaluate, programs))
```

Provide the setup script. For cluster use, pass the same target to both the
builder and pool so the controller can resolve the saved baseline.

## Share cluster capacity

```python
pool = Pool(target="lab", name="coding", size=16, warm=4,
            weight=2, priority=0, placement="spread", detached=True)
pool.start()
```

Cluster pool `weight` controls its share of available capacity under contention.
`priority` orders placement requests. `spread` distributes work across workers;
`pack` prefers workers already in use. These settings do not preempt an active
episode. They are separate from a sandbox's CPU scheduling weight.

## Reuse images across workers

These options require Sandweave 0.2.7 or newer on the client, controller and workers.

```python
with Pool(target="lab", template="coding", size=32, warm=8,
          shared_cache="/shared/sandweave", affinity="machine") as pool:
    results = list(pool.map(evaluate, programs))
```

`shared_cache` is an absolute directory on the workers. Sandweave creates it if
needed. Workers must have read and write access using the same account. A shared
filesystem lets them reuse one baseline and its image files without sending the
payload through the controller. With node-local storage, workers on each node
reuse that node's copy; Weave transfers the baseline once per cache. The path
does not change worker state directories or share writable sandbox files.

With 0.2.15 or newer on controllers and workers, sandboxes waiting for the same
image share its preparation work without occupying launch threads. Other
launches and cleanup can proceed while that image is being published.

With controller 0.2.19 or newer, a worker that already has the baseline starts
from its existing revision. Shared-cache publication is needed only when a
worker is missing that revision.

Local pools, including pools with explicit `targets`, also accept `shared_cache`.
By default, closing a pool keeps the cache for reuse. Creating separate pools still creates
separate baselines unless they use the same saved `cache` or `snapshot`.
Workers may link or copy cached files into their runtime workspace; this setting
does not guarantee zero disk copies across filesystems.

For cluster pools, `affinity="machine"` prefers workers on the same machine as
the pool's initial placement. `affinity="worker"` prefers the same worker.
Both spill over when resources, labels, or worker availability require it.
`placement` controls spreading or packing within the preferred location.
The default `affinity=None` preserves the usual placement behavior.
The initial builder remains the affinity reference after it terminates,
including across controller restarts, and no longer reserves resources.

A machine is a running Linux kernel, identified by its boot ID. Separate workers
and PID namespaces can belong to it. If that ID is unavailable or masked,
Sandweave does not infer shared hardware from matching hostnames. Worker listings
include `machine`; `pool.info` includes `affinity` and `shared_cache`.
Use `pool.update(affinity="worker")` to change future placement. The cache path
is fixed when the pool is created.

## Reconnect to a named pool

```python
from sandweave import Pool

with Pool.connect("coding", target="lab") as pool:
    with pool.acquire() as env:
        print(env.run("python --version").stdout)
```

Connecting borrows a handle. Closing that borrowed handle leaves the pool
running. Use `pool.terminate()` to end a cluster pool. A context that creates a
pool terminates it on exit, including when `detached=True`.

Use a [job](jobs.md) for a submitted command that should run independently of your
Python callback and retain its result.

## Release pool files

For a cluster pool whose prepared files are needed only for one run:

```python
with Pool(target="lab", image="docker://python:3.12-slim",
          size=8, warm=2, retain_baseline=False) as pool:
    results = list(pool.map(evaluate, programs))
```

This requires Sandweave 0.2.13 or newer on the client, controller and workers.
After the sandboxes terminate, closing the pool removes its generated snapshots,
downloaded image files and private writable workspaces. Shared caches use a
pool-specific subdirectory. Previously prepared images can be linked or copied
into that directory without rebuilding; their original copies remain available.
Shared runtime installations, external volumes and other pools' files remain.

Use this option with a new gVisor baseline. It cannot be combined with `cache`,
`snapshot`, `cache_key`, or `keep_on_error`. Local pools retain their files.
The default `retain_baseline=True` preserves the existing behavior.

If another sandbox, pool or named cache still needs the baseline, cleanup waits
and `pool.info["cleanup_error"]` explains what retains it. Otherwise, closure
waits for reclamation and `pool.info["artifacts_released"]` becomes `True`.
The controller retries interrupted cleanup; a close timeout leaves that request
active. Small lifecycle records and release markers remain for recovery.

Automatic eviction by age or storage size is not implemented. Pool size bounds
live environments; `retain_baseline=False` bounds the lifetime of its own files.

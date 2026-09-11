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

```python
with Pool(target="lab", size=8, warm=2) as pool:
    with pool.acquire() as env:
        print(env.run("python --version").stdout)
    pool.update(size=16, warm=4)
```

`target="lab"` uses [Weave](clusters.md) to place work across registered workers.
A printed cluster address works too. A local pool needs no controller setup.

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

Local pools, including pools with explicit `targets`, also accept `shared_cache`.
Closing a pool keeps the cache for reuse. Creating separate pools still creates
separate baselines unless they use the same saved `cache` or `snapshot`.
Workers may link or copy cached files into their runtime workspace; this setting
does not guarantee zero disk copies across filesystems.

For cluster pools, `affinity="machine"` prefers workers on the same machine as
the pool's initial placement. `affinity="worker"` prefers the same worker.
Both spill over when resources, labels, or worker availability require it.
`placement` controls spreading or packing within the preferred location.
The default `affinity=None` preserves the usual placement behavior.

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

Closing a pool releases its environments. It retains saved baselines and image
files for reuse. Automatic eviction by age or storage size is not implemented;
pool size bounds live environments, not retained disk usage.

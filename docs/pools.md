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

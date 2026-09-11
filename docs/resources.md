# CPU, memory and GPUs

Override resources at creation time. You do not need a new template:

```python
from sandweave import Sandbox

with Sandbox(cpu=4, memory="8GiB") as env:
    print(env.run("python --version").stdout)
```

The same arguments work with `target="lab"` or a printed cluster address.

## CPU sharing

```python
from sandweave import CPU, Sandbox

with Sandbox(cpu=CPU(vcpus=2, weight=200, quota=1.5)) as env:
    print(env.info["cpu"])
```

| Setting | Meaning |
| --- | --- |
| `vcpus` | Virtual CPU count advertised to the sandbox. |
| `weight` | Relative share of CPU time under contention; default 100. |
| `quota` | CPU-time budget measured in cores; `1.5` allows about 1.5 cores of CPU time. |

gVisor enforces weights and quotas through a sampled userspace controller.
These settings share the worker's eligible CPUs; they do not reserve dedicated
physical cores or grant more CPUs than the host allocation supplies.

## Guest and runtime memory

```python
from sandweave import Memory, Sandbox

with Sandbox(memory=Memory(guest="4GiB", runtime="1GiB")) as env:
    print(env.info["memory"])
```

**Guest memory** is the budget for sandbox application pages. **Runtime memory**
is a separate guard for the processes implementing the sandbox. Both count
toward cluster admission. Neither is the host machine's total memory.

A string such as `memory="8GiB"` overrides guest memory and retains the template's
runtime budget. Sizes can also be supplied as integer byte counts.

A 32 GiB worker fits three sandboxes configured with 8 GiB guest memory and
512 MiB runtime memory. With 256 MiB for each budget, it fits up to 64, subject
to its slot limit and other reservations. Slots do not grant additional memory.

Worker budgets are capped at visible cgroup hard limits, including narrower job
steps and ancestor limits. An environment variable cannot raise that ceiling.
If several workers or unrelated processes share a memory allocation, their
budgets still need to fit within that shared allocation.

## Use a GPU

```python
from sandweave import Sandbox

with Sandbox(template="cuda", gpu=True) as env:
    print(env.run("nvidia-smi").stdout)
```

Use a model name to select among eligible devices:

```python
with Sandbox(template="cuda", gpu="L40S") as env:
    print(env.info["gpus"])
```

The worker must already have access to that GPU. Weave currently reserves whole
GPUs for cluster admission; fractional GPU admission is not implemented.

## Inspect configuration

```python
from pprint import pprint
from sandweave import Sandbox

with Sandbox() as env:
    pprint(env.info)
```

The summary includes identity, state, template, worker hostname, CPU and memory
settings, selected GPU devices, and VNC details where applicable. Resource
settings are budgets, not current usage. The [dashboard](dashboard.md) shows
live measurements separately.

## Choose a runtime

gVisor is the default. For code using the host kernel directly:

```python
with Sandbox(runtime="apptainer") as env:
    print(env.run("python --version").stdout)
```

Native Apptainer supports commands, files, GPU exposure, pause/resume, and
filesystem caches. It uses the host network and does not support CPU weights,
CPU quotas, offline networking, or memory snapshots.

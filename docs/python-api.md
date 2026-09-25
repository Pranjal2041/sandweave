# Python API

Import the public objects from `sandweave`:

```python
from sandweave import Sandbox, Pool, Cluster, Job, Benchmark
from sandweave import CPU, Memory, GPU, Network, Template, SnapshotRef, Mount, Slurm, Recording
from sandweave import create_service_network, delete_service_network
```

Methods that support async calls expose `.aio`. See [async Python](async.md).

## Sandbox

```python
env = Sandbox(template="coding", cpu=2, memory="4GiB", network="offline")
```

### Creation options

| Argument | Meaning | Default |
| --- | --- | --- |
| `template` | Built-in name, TOML file, or `Template`. | `"coding"` |
| `image` | `docker://` base image; can accompany a template. | Template image, if set |
| `setup` | Client-side path to a guest setup script. | None |
| `cache` | Saved filesystem cache name or reference. | None |
| `snapshot` | Saved snapshot reference. | None |
| `cache_key` | Reuse matching prepared recipe state. | None |
| `refresh` | Resolve an image tag again and rebuild named setup caches. | `False` |
| `cpu` | Virtual CPU count or `CPU(...)`. | Template default |
| `memory` | Guest size or `Memory(...)`. | Template default |
| `gpu` | Boolean, model name, or `GPU(...)`. | Template default |
| `network` | `"internet"`, `"offline"`, or `Network(...)`. | Template default, normally `"internet"` |
| `service_network` | ID returned by `create_service_network(target=...)`. Pins placement to its worker. | None |
| `aliases` | DNS names visible to members sharing a named network. | `[]` |
| `networks` | Named networks this member joins within the service network. | `["default"]` |
| `target` | Cluster name/address, worker target, or allocation. | Local worker |
| `runtime` | `"gvisor"` or `"apptainer"` for a new recipe. | `"gvisor"` |
| `env` | Guest environment variables. | Template environment |
| `mounts` | Explicit worker-path mounts. | None |
| `name` | User-supplied sandbox name. | None |
| `ttl` | Seconds of lifetime after readiness. | No TTL |
| `detached` | Survive the creating Python process exiting. | `False` |
| `recording` | Record an Xvnc desktop; accepts `True` or `Recording(fps=15, cursor=True)`. | `False` |
| `startup_timeout` | Sandbox creation deadline in seconds. | `300` |
| `keep_on_error` | Retain a failed sandbox for diagnosis, subject to its lifetime. | `False` |
| `experimental_gpu_live` | Opt into qualified experimental CUDA memory restore. | `False` |

`cache` and `snapshot` are alternative sources. A saved source cannot be combined
with `template`, `image`, `setup`, or `cache_key`. A restore retains its saved runtime and image.

### Commands

```python
result = env.run("python --version", timeout=5, check=False)
process = env.exec("python -u /workspace/main.py", timeout=60)
```

Both accept `cwd`, `env`, `user`, `timeout`, `shell`, `binary`,
`max_output_bytes`, and `pty`. Their default `cwd` is the template's `workdir`,
the image's working directory, or `/workspace` for existing templates. Use `argv`
instead of a command string for literal arguments. `check` is a `run` option;
use `process.wait(check=True)` for an `exec` process.

`run` returns `stdout`, `stderr`, and `returncode`. `exec` returns a `Process`
with streams, `wait`, `poll`, `result`, `terminate`, and terminal `resize`.

### Files and preparation

| Method | Purpose |
| --- | --- |
| `env.files.read_text(path)` / `write_text(path, text)` | Text files. |
| `env.files.read_bytes(path)` / `write_bytes(path, data)` | Binary files. |
| `env.files.upload(source, destination)` | Client to guest transfer. |
| `env.files.download(source, destination)` | Guest to client transfer. |
| `env.files.open(path, mode="r")` | Buffered file stream. |
| `env.files.stat(path)` / `list(path)` | Inspect guest files and directories. |
| `env.setup(path, inputs=(), user="root")` | Execute a declared setup script inside the guest. |

### State and lifetime

| Member | Purpose |
| --- | --- |
| `env.id` | Sandbox identifier. |
| `env.info` | Fresh state, configured resources, worker, GPU and VNC summary. |
| `env.timings` | Recorded startup durations. |
| `env.spec` | Creation specification. |
| `env.status()` | Detailed current record. |
| `env.cache(key, state="filesystem")` | Save a named cache and return a `SnapshotRef`. |
| `env.snapshot(state="memory")` | Save a snapshot and return a `SnapshotRef`. |
| `env.pause()` / `resume()` | Suspend or continue the resident environment. |
| `env.stop(state="auto")` | Save, then release the runtime. |
| `env.terminate()` | Release the runtime and discard unsaved state. |
| `env.close()` | Disconnect the handle; a benchmark-owned handle also releases its task lease. |
| `env.recording.info` | Recording segments, capture timings, drops and worker storage. |
| `env.recording.stop()` | Finalize recording, leaving the desktop running. |
| `env.recording.download(directory)` | Finalize and export to an empty client directory; works after termination. |
| `env.recording.delete()` | Delete finalized recording files from the worker. |
| `Sandbox.connect(id, target=...)` | Borrow a handle to an existing sandbox. |
| `Sandbox.create.aio(...)` | Asynchronous creation. |

`SnapshotRef.verify()` waits for content verification and returns a status
record. Check that `status == "passed"` before relying on the saved state.

### Workload controls

`env.desktop` exposes the desktop controls provided by its template.
For Xvnc templates, `env.desktop.vnc()` opens a byte stream through the SDK
transport. The stream has `read(n=65536)`, `write(data)`, `close()` and `.aio()`
equivalents. It works on connected handles and local, SSH or Weave targets.
`env.vr` exposes VR controls. A template must supply a control before it can be
used. See [desktop agents](desktop.md), [VR games](vr.md), and
[templates](templates.md).

## Service networks

`create_service_network(target=None)` returns a network ID. Pass that ID and the
same target to `Sandbox(service_network=id, aliases=[...], networks=[...])`.
`delete_service_network(id, target=None)` deletes an empty network; terminate its
members first. `env.info["service_network"]` reports membership and its fixed
service address. See [connecting sandboxes](networking.md#connect-sandboxes).

## Resource values

```python
cpu = CPU(vcpus=2, weight=100, quota=1.5)
memory = Memory(guest="4GiB", runtime="1GiB")
gpu = GPU(model="L40S")
network = Network(mode="offline")
```

These values can be passed to `Sandbox` and pool creation. See
[resources](resources.md) for units, admission, and runtime support.

`Memory(guest="16GiB", reservation="4GiB", experimental=True)` opts into
[experimental memory sharing](resources.md#experimental-memory-sharing).
The reservation affects admission; the guest limit remains 16 GiB. Combined
usage can exceed physical RAM and cause host out-of-memory failures.

`Network(proxy=...)` accepts a proxy URL or a list of URLs. See
[proxy networking](networking.md#use-a-proxy) for selection, setup and application support.

`ProxyPolicy(distribution="random", region=None)` supports `random`, `round_robin`,
`same_proxy` and `same_region`. Pass it as `Network(proxy=proxies, policy=...)`.
`proxies` also accepts a mapping from region labels to URL lists. See
[proxy policies](networking.md#proxy-policies) for standalone and pool semantics.

## Cluster

| Member | Purpose |
| --- | --- |
| `Cluster.start(name, local_worker=True, ...)` | Start or reconnect to a local controller. |
| `Cluster.connect(name_or_address, ...)` | Connect to a saved name, HTTP(S), or SSH address. |
| `cluster.workers` / `cluster.info` | Worker inventory and cluster status. |
| `cluster.add_worker(target, ...)` | Register an existing worker target. |
| `cluster.drain(id)` / `resume(id)` | Disable or allow new placement. |
| `cluster.remove_worker(id)` | Unregister a drained worker after reservations are released. |
| `cluster.events(after=0, limit=100)` | Read cluster events. |
| `cluster.dashboard()` | Create a short-lived browser sign-in link. |
| `cluster.backup(path)` | Save a controller database backup. |
| `cluster.stop()` | Stop the controller, retaining its state. |
| `cluster.close()` | Disconnect the client handle. |

`Cluster.connect` accepts `token`, `token_file`, and `ca_file`. A complete printed
HTTP link already includes authentication. See [clusters](clusters.md).

## Pool

```python
pool = Pool(target="lab", template="coding", size=8, warm=2)
```

Use `pool.start()` or a context manager to start it. `pool.acquire()` leases an
environment; `pool.map(callback, items)` maps work over independent environments.
Pools accept `shared_cache`, an absolute image/snapshot cache directory on the
workers. Cluster pools also accept `weight`, `priority`, `labels`, `placement`,
and `affinity` (`"worker"`, `"machine"`, or `None`), and support `update(...)`.
Affinity prefers a location and allows spillover. The cache path is fixed at
creation; the other placement settings can be updated for future assignments.
`Pool.connect(name, target=...)` borrows a named cluster pool.
See [pools and evaluation](pools.md) for ownership and cleanup.

## Benchmark

Available in the `0.2.20rc3` preview. See [benchmarks](benchmarks.md) for installation and OSWorld parity.

```python
bench = Benchmark("osworld-energy50-representative", capacity=4)
```

`capacity` limits concurrent tasks; `preload` sets the initial ready reserve and
defaults to capacity. `source` selects a reference checkout. Other options pass
through to the pool, including `target`, resources and `shared_cache`.

| Member | Purpose |
| --- | --- |
| `bench.start()` / `close()` | Prepare or release the pool and evaluator resources. |
| `next(bench)` / `bench.next(timeout=None)` | Acquire the next prepared task; wait for capacity, or raise `StopIteration` at exhaustion. |
| `for task in bench` | Iterate over task attempts. |
| `with task as env` | Acquire and prepare a clean sandbox; release it on exit. |
| `task.env` | Access an already acquired task's sandbox. |
| `task.close()` | Release the sandbox and pool lease; safe to repeat. |
| `task.id` / `task.instruction` | Task identity and agent instructions. |
| `task.evaluate()` | Return an `Evaluation` while the task sandbox is leased. |
| `bench.task(id)` | Create a new attempt for one task. |
| `bench.map(agent)` | Run `agent(env, instruction)` concurrently and yield ordered evaluations. |
| `bench.tasks` / `bench.results` | Task specifications and latest completed evaluations. |

`Evaluation` contains `task_id`, `score`, `passed` and `feedback`.
Async contexts and `.aio` methods follow the same lifecycle.
`await bench.next.aio()` raises `StopAsyncIteration` at exhaustion.
A positive `timeout` bounds checkout waiting, including an async acquisition
queue; it excludes initial pool preparation and task setup. A timed-out or
cancelled checkout is available for a later pull. Setup failures release the
lease and propagate; use `bench.task(id)` for an explicit retry. The pull cursor
is shared between concurrent callers. Existing lazy iteration and `map` each
continue to traverse the full task list independently.

## Job

```python
job = Job.submit("python -c 'print(42)'", target="lab", detached=True)
```

Use `Job.connect(id, target=...)`, `job.info`, `job.wait(...)`, `job.result(...)`,
`job.cancel()`, and `job.close()` to manage a submitted job. See [jobs](jobs.md)
for uploaded files, batches, deadlines, retries, and output limits.

## Errors

| Error | Meaning |
| --- | --- |
| `CommandError` | A nonzero exit when `check=True`; includes `.result`. |
| `CommandTimeout` | Execution deadline exceeded. |
| `OutputLimitExceeded` | Captured output exceeded its limit. |
| `SetupError` | Guest setup failed. |
| `CacheMiss` / `CacheConflict` | Missing saved state or a conflicting cache publication. |
| `IncompatibleSnapshot` | Saved state is incompatible with the requested restore. |
| `UnsupportedFeature` | The selected runtime or template does not support an operation. |
| `ResourceUnavailable` | Required worker resources or infrastructure are unavailable. |
| `OperationUnknown` | A connection failed after delivery may have happened. Do not blindly replay a mutation. |

Public SDK failures derive from `SandboxError`. Some validation errors use
standard Python exceptions such as `ValueError` or `FileNotFoundError`.

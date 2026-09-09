# Using Sandweave

The Python SDK and CLI use the same lifecycle. A template defines setup,
services and controls; a sandbox is one running instance. Start with the
[README examples](../README.md#agreed-public-api-contract-v1).

## Install and configure

The SDK host needs Python 3.11 or newer. Workers currently need Linux x86-64,
Apptainer and prepared runtime files. GPU workers also need a compatible NVIDIA
device and driver. Run setup on each worker:

```bash
python -m pip install .
sandweave setup
sandweave doctor
sandweave run --template coding -- "python -c 'print(2 + 2)'"
```

Setup asks which workload to prepare, finds an existing runtime installation,
offers dependency repairs, stages worker files and tests a disposable coding
sandbox. Selecting desktop or VR offers the Python packages that workload
needs. Setup does not launch a game or acquire a GPU job.

Doctor opens a terminal menu with the checks and available repairs. It can
save a runtime location, install missing Python packages, register an existing
Apptainer executable or run its upstream installer, and install FFmpeg for VR.
Each repair is followed by another check. Declining a proposed repair leaves it
unapplied. Interrupting an installation can leave downloaded files or installed
packages; run doctor again to inspect the result. A failed required check
produces a nonzero exit status.

```bash
# Prepare coding without prompts, accepting the available repairs.
sandweave setup --yes --template coding

# Prepare the dependencies for desktop or VR use.
sandweave setup --template gnome
sandweave setup --template vr/gunspinning

# Check without changing configuration or installing anything.
sandweave doctor --check
sandweave doctor --json
```

The workload selection controls setup and diagnostics. It does not change the
default template used by `Sandbox()`. Setup and doctor inspect the machine on
which they run. Host restrictions, unavailable GPUs and missing prepared images
can remain unresolved. Doctor does not change host security policy, use sudo,
or cancel Slurm jobs.

The current rootless launcher uses Apptainer's `--userns` mode. User namespaces
let an ordinary account create containers without host root privileges. Doctor
tests the actual Apptainer launch, including hosts where a security profile
permits Apptainer but blocks a generic namespace probe. See
[Apptainer's requirements](https://apptainer.org/docs/admin/main/user_namespace.html).

VR video export requires FFmpeg with `libx264` on the Python client. Doctor can
install `imageio-ffmpeg` and register its binary for Sandweave; the `[vr]` Python
extra alone does not install FFmpeg. Managed executables live under
`SANDWEAVE_HOME/bin`; new Sandweave workers and VR exporters find them without
editing shell startup files. The Apptainer installer needs `bash`, `curl`,
`rpm2cpio` and `cpio`; if these are unavailable, the menu can register an existing
installation instead.

From this checkout, runtime files are discovered automatically. Setup saves the
location for later use from other directories. For automation, use
`sandweave setup --assets /path/to/prepared-assets --yes`. The older
`configure --assets` command and `SANDWEAVE_ASSETS` remain supported; the
environment variable takes precedence over saved configuration.
Changing that selection uses a new worker workspace for new connections.
Existing workers and handles keep their prepared runtime files.

`SANDWEAVE_HOME` selects the private worker, metadata and artifact directory;
its default is `~/.local/share/sandweave`. Durable snapshots belong on durable
storage. Runtime working directories are separate and node-local. Setup does
not change the original lab's `runs/local-path.txt`.

The wheel contains the engine scripts. Large images, runtime binaries, GPU
drivers, game downloads and saved application bases remain external assets.
There is no published runtime download bundle yet; setup cannot install the
base images on a fresh machine without an existing prepared installation.
See [lab reproduction](gvisor-lab-reproduction.md) for their preparation.

`sandweave setup ID SCRIPT` keeps its earlier meaning: run a script inside an
existing sandbox. With no ID or script, `sandweave setup` prepares the worker.

## Commands

`run` and `exec` each take one command string. By default it runs through
`/bin/sh -c` inside the guest. Templates can set `command_shell`, and
`shell="/bin/bash"` selects another shell for one call. Shell quoting, expansion,
pipes and redirects follow that shell; there is no implicit `errexit` or
`pipefail`. Calls use a noninteractive, non-login shell by default.

Each call starts a fresh process. `cwd` and `env` apply to that call; `cd` or
`export` in an earlier command do not persist. `run` waits and checks the exit
status unless `check=False`. `exec` returns a process; use `wait(check=True)` to
raise on its nonzero exit. Command timeouts raise typed errors.

For direct execution without a shell, pass a nonempty `argv` list instead of a
command string: `env.run(argv=["python", "main.py"])`. The forms are mutually
exclusive, and `shell` cannot accompany `argv`. The CLI accepts one quoted
command string after `--`, or `--argv -- PROGRAM ARG ...` for literal arguments.

## Templates

| Template | Provides |
| --- | --- |
| `coding` | Python, shell, command/file execution; the default. |
| `gnome` | GNOME/Xvnc, desktop actions and screenshots. |
| `cuda` | Coding environment with one eligible NVIDIA GPU. |
| `docker` | A guest Docker daemon and service readiness. |
| `vr/opensaber` | Open Saber, Monado, virtual controllers and both eye views. |
| `vr/gunspinning` | GunSpinning's VR motion-controller mode and paired recording. |
| `games/gunspinning-gamepad` | GunSpinning's flat gamepad mode, SDL input and desktop output. |

VR recipes require the configured `vr-base@1` prepared asset. Gamepad flat mode
does not produce a stereo observation. VR recordings save lossless eye pairs
and export lossy left, right and synchronized side-by-side MP4 previews.
Video export requires at least two recorded frames; ending an episode
immediately can leave the recording too short to export.

A local TOML recipe can extend a built-in template:

```toml
extends = "gnome"

[setup]
script = "install.sh"
inputs = ["requirements.txt"]

[services.my_app]
command = "python /workspace/app.py"
ready = { exec = "curl -fsS http://localhost:8000/health" }

[capabilities.desktop]
resolution = [1920, 1080]
```

Setup files are copied into the guest. Services start on each cold boot; memory
restore preserves their existing processes. Setup inputs must be explicit.
The overall startup deadline includes setup, service and control readiness.
`keep_on_error=True` retains a failed guest for diagnosis using the exception's
`sandbox_id`; it does not return an environment claimed to be ready.

`env.exec("bash", pty=True)` opens a real guest terminal; `process.resize(rows,
cols)` adjusts its size. Terminal stdout/stderr share one channel, with normal
terminal echo and line endings. Closing terminal stdin sends canonical EOF;
raw-mode applications may require `terminate()`. `sandweave shell ID` uses this
path. Open terminal state is included in supported gVisor memory checkpoints.

Guest files support sync and async stream contexts, for example
`async with await env.files.open.aio("/workspace/data", "w") as stream`, followed
by `await stream.write.aio(...)`. Directory transfers preserve empty directories;
symlinks require explicit handling rather than implicit traversal.

Third-party controls register `sandweave.controls.v1` entry points. The
[separately installed point-mass example](../examples/pointmass/README.md)
demonstrates the contract without editing Sandbox. Provider versions must match
between client and worker. Runtime extensions use `sandweave.runtimes.v1` and
must implement the runtime operations and compatible saved-state manifests.
These are trusted extensions running inside the worker process.

## Saved state and ownership

`cache` captures filesystem state by default; `snapshot(state="memory")`
captures supported process/kernel state. Each clone gets independent writable
state. Names can move, while returned references pin a revision. Preparation
keys include the recipe, declared setup inputs, engine/SDK/provider code and
asset registry. A missing cache raises instead of silently rebuilding it.

Snapshot verification is asynchronous and explicit `reference.verify()` waits
for a complete content check. Check that its returned `status` is `"passed"`;
an integrity mismatch returns `"failed"`. Error details depend on the runtime.
Finish verification before relying on a snapshot after its source node
disappears: initial verification of an unhashed capture
can still need that node's frozen source. Completed artifacts can be imported
by another worker with access to the shared store and recorded dependencies.

`stop()` saves before terminating; a failed save preserves the source.
`terminate()` discards unsaved state, while existing artifacts and external
volumes remain. `close()` disconnects only. An owned context terminates on exit;
a `Sandbox.connect(...)` context is borrowed and only disconnects. TTL counts
wall time from readiness, including pauses and disconnected clients.

Read-only worker-path mounts use `Mount(source, destination)`. External writable
mounts require an explicit capture policy: `snapshot="rebind"` keeps shared
external state on its own timeline; otherwise capture is rejected.

## Placement and pools

```python
from sandweave import Sandbox, Slurm

allocation = Slurm.connect("12345")  # Borrow an existing job.
with Sandbox(target=allocation) as env:
    print(env.run("hostname").stdout)
```

`Slurm.acquire(...)` explicitly creates an owned allocation. Its context closes
only that allocation. An existing-job worker runs in its own persistent Slurm
step with the step's CPU/GPU eligibility. The current Slurm adapter requires
shared access to SDK metadata/assets. Generic SSH can start a worker or connect
to its private metadata file. Target dictionaries can be saved with
`sandweave targets add NAME --config '{"job_id":"12345"}'`.

Pools pin one baseline, bound concurrent leases and discard used episodes.
`targets=[...]` distributes independent episodes across workers. Async callbacks
use `async for result in pool.map.aio(callback, inputs)`; sync callbacks stay in
caller-side threads. Cancellation drains owned work before releasing its guest.
Python cannot forcibly interrupt an arbitrary blocking host callback.

```bash
sandweave pool create --template coding --size 4 --warm 2 --name coding-pool
sandweave pool exec coding-pool -- "python -c 'print(42)'"
sandweave pool status coding-pool
sandweave pool close coding-pool
```

Named CLI pools belong to their worker and survive individual CLI invocations.
They are resident state, not automatically reconstructed pools after worker loss.
CLI-owned allocations can be managed with `slurm acquire/status/close`; `close`
refuses to cancel jobs that were not created through that CLI.

## Limits that matter

- CPU weights and quotas are sampled userspace scheduling within eligible CPUs.
  Guest memory and the sampled runtime guard are separate budgets. Native
  Apptainer uses a sampled aggregate RSS guard and has fewer controls.
- Native Apptainer supports commands, files, GPU exposure, pause/resume and
  filesystem capture. It uses the host kernel/network and one mapped UID;
  filtered networking, CPU weights and memory snapshots fail explicitly.
- GPU filesystem capture starts fresh GPU processes. Ordinary graphics RAM
  restore is unsupported. CUDA-only RAM capture/restore requires explicit
  `experimental_gpu_live=True` at both ends. MPS remains cooperative and
  experimental; it does not isolate graphics or memory bandwidth.
- Desktop actions acknowledge the input server, not application repaint. VR
  actions acknowledge the runtime, not game consumption or a simulation tick.
  Application FPS and stereo capture cadence are different measurements.
- Command output has a 64 MiB combined spool budget by default. Set
  `max_output_bytes` per command or in template runtime options. Exceeding it
  terminates that command group and raises `OutputLimitExceeded` with partial
  output. Inline results retain up to 1 MiB per stream and identify truncation
  and guest-backed output references. These references share the guest lifetime.
- No automatic command/action replay, transparent worker failover, automatic
  artifact eviction, incremental checkpointing or live graphics restoration is
  claimed. External services and shared volumes do not roll back with a guest.
- Xvnc remains the SDK's desktop control backend. The separate experimental
  Wayland lab path is preserved; it has not acquired Xvnc's fast-I/O guarantees.

Use [current acceptance](sdk-implementation-progress.md) for measured behavior.
Cold startup is not a few milliseconds, and a ready pool handle's checkout time
must not be substituted for checkout plus the first useful command.


## Run acceptance

`python -m pytest tests` runs SDK host checks; integration modules are explicitly
skipped unless enabled. Inside a disposable allocation with configured assets:

```bash
SANDWEAVE_HOME="$PWD/runs/my-sdk-tests" \
SANDWEAVE_ASSETS="$PWD" \
SANDWEAVE_INTEGRATION=1 SANDWEAVE_GPU_INTEGRATION=1 \
python -m pytest tests -v
```

The full GPU suite used 12 eligible CPUs, 96 GiB of allocated memory and an L40S.
It intentionally creates and destroys its own sandboxes and exercises a
controller-failure drill restricted to its isolated worker. Run it in a dedicated
SDK home, not a worker that owns active user environments. Without the GPU flag,
CUDA/MPS and VR tests are explicitly skipped. Historical engine unit suites are
recorded separately in the [acceptance report](sdk-acceptance.json).

Additional scripts reproduce the application and placement checks:
`sdk-cua-harness.py`, `sdk-gunspinning-demo.py`, `sdk-scale-acceptance.py`,
`sdk-multiworker-acceptance.py` and `sdk-installed-acceptance.py` in `scripts/`.
Each exposes its allocation/output options through `--help`. The installed-wheel
check expects the separately installed point-mass example package.

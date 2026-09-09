# Using Sandweave

The Python SDK and CLI use the same lifecycle. A template defines setup,
services and controls; a sandbox is one running instance. Start with the
[README examples](../README.md#agreed-public-api-contract-v1).

## Install and configure

`Sandbox()` and `Sandbox(template="gnome")` prepare missing local dependencies
on first use. The CLI's `create` and `run` commands use the same path. Templates
that extend a built-in template install that parent's dependencies too. First
use keeps your selected storage directory; without a saved choice it uses
`.sandweave` in the current directory. `sandweave setup` is available to choose
storage and install a template before creating a sandbox.

The SDK host needs Python 3.11 or newer. Workers need Linux x86-64, Bash and
permission to run containers. Setup installs Apptainer if needed and prepares
the runtime files. GPU workers also need an allocated NVIDIA device and a
compatible driver. From a checkout, inside your Python environment:

```bash
uv pip install .
sandweave setup
sandweave doctor
sandweave run --template coding -- "python -c 'print(2 + 2)'"
```

Setup asks what you want to start with and where to store Sandweave's files.
The storage directory can be empty. It imports a usable existing runtime or
builds one from upstream inputs, installs the selected workload's Python
packages, and checks a disposable sandbox of that template. Selecting VR
therefore starts the game for the check. Setup releases its test sandbox;
it does not acquire or cancel GPU jobs. `python -m pip install .` also works.

Doctor opens a terminal menu with the checks and available repairs. It can
install or repair runtime files, install missing Python packages, register an existing
Apptainer executable or run its upstream installer, and install FFmpeg for VR.
Each repair is followed by another check. Declining a proposed repair leaves it
unapplied. Interrupting an installation can leave downloaded files or installed
packages; run doctor again to inspect the result. A failed required check
produces a nonzero exit status.

```bash
# Prepare coding without prompts, accepting the available repairs.
sandweave setup --yes --template coding --directory /path/to/sandweave-data

# Prepare the dependencies for desktop or VR use.
sandweave setup --template gnome
sandweave setup --template vr/gunspinning

# Check without changing configuration or installing anything.
sandweave doctor --check
sandweave doctor --json
```

The workload selection controls what setup installs and doctor checks. It does
not change the default template used by `Sandbox()`. Creating another local
template installs it as needed; repeating setup prepares it ahead of time.
Setup and doctor inspect the machine on which they run.
Doctor reports host policy restrictions and unavailable GPUs; it cannot grant
permissions or allocate hardware. It does not use host sudo.

The current rootless launcher uses Apptainer's `--userns` mode. User namespaces
let an ordinary account create containers without host root privileges. Doctor
tests the actual Apptainer launch, including hosts where a security profile
permits Apptainer but blocks a generic namespace probe. See
[Apptainer's requirements](https://apptainer.org/docs/admin/main/user_namespace.html).

VR video export requires FFmpeg with `libx264` on the Python client. Doctor can
install `imageio-ffmpeg` and register its binary for Sandweave; the `[vr]` Python
extra alone does not install FFmpeg. Managed executables live under
`SANDWEAVE_HOME/bin`; new Sandweave workers and VR exporters find them without
editing shell startup files. The private Apptainer installer uses Bash and basic
Unix utilities. Sandweave supplies private download and package-extraction
adapters when `curl`, `rpm2cpio` or `cpio` is missing. Python package installation
uses the active interpreter, including uv environments without pip.

Setup can discover a runtime in this checkout or import one you provide:

```bash
sandweave setup --assets /path/to/existing-runtime --directory /path/to/new-storage --yes
```

`--assets` reads runtime files from an existing installation. `--directory` is
where setup writes the new installation. The older
`configure --assets` command and `SANDWEAVE_ASSETS` remain supported; the
environment variable selects the source ahead of saved configuration. When
first use extends that source, Sandweave records and reuses the resulting
installation without modifying the source directory or the variable.
Changing that selection uses a new worker workspace for new connections.
Existing workers and handles keep their prepared runtime files.

Sandweave stores downloads, tools, runtime files, worker state and caches directly
under the selected storage directory. The initial suggestion is `.sandweave`
under the current directory; later setup runs suggest the saved location.
You can choose another empty directory. Setup prints the destination before
installing anything. It must be writable and belong to you.

After the sandbox check succeeds, setup saves the configuration and a small
`~/.local/share/sandweave/location.json` pointer so commands work from other
directories. A failed check leaves the previous saved configuration intact.
Completed runtime inputs are recorded for retry. Failed build work and logs
stay in the printed destination for diagnosis; setup does not automatically
evict them.

Automatic installation checks the runtime files and software prerequisites,
then saves the installation. The requested sandbox supplies the actual launch
and control-readiness check; it does not create a second disposable sandbox.
Installation runs in a separate process, under the same storage lock as setup.
Progress goes to stderr and to a file under `logs/setup`. Repeating a constructor
after interruption retries installation. Existing runtime workers keep their
files. Reuse checks required files without rehashing entire images; installation
and initial worker staging still verify file contents.

Set `SANDWEAVE_HOME` to use a different data directory. This explicit setting
takes precedence, including during setup. Setup preserves files in the earlier
`~/.local/share/sandweave` default; it does not move or delete existing sandboxes or
snapshots. To access them, set `SANDWEAVE_HOME` to that directory.

Durable snapshots belong on durable storage. Active runtime working directories
are separate and node-local. Staging uses hardlinks on the same filesystem;
copies across filesystems are published only after completion. Incomplete
staged files are checked and repaired when preparing a worker. Setup does not
change the original lab's `runs/local-path.txt`.

The wheel includes an explicit set of engine scripts, build inputs and patches.
It does not include large runtime images. Without a prepared source, setup
downloads container images and pinned source inputs, builds the patched gVisor
engine in an unprivileged build container, and installs guest software inside
gVisor. Guest root privileges never become host root privileges. Network helpers
and their dependencies are extracted into the installation when needed.
The first source build needs internet access, disk space for compiler output
and image exports, and enough memory for the selected guest workload. Build
logs are written under `logs/setup` in the selected storage directory.

Open Saber is downloaded automatically. GunSpinning's official site supplies
its Linux ZIP through a download page, so setup asks you to select that file.
For automation, pass `--game-archive /path/to/gunspinning-vr-linux.zip`. Setup
checks the pinned game version and preserves the original archive. The native
Apptainer runtime downloads its base container on first use if the selected
runtime did not already include it.

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

## Inspect a sandbox

`env.info` returns a fresh dictionary on each access. `sandweave info ID`
prints the same fields as JSON; a unique sandbox name also works.

| Field | Meaning |
| --- | --- |
| `id`, `name`, `state` | Identity and current lifecycle state. |
| `template`, `runtime` | Resolved template name and chosen runtime. |
| `worker` | Worker `hostname` and `job_id` (`None` outside Slurm). |
| `cpu` | Configured `vcpus`, sharing `weight` and optional `quota`. |
| `memory` | Configured `guest` and `runtime` budgets, in the units supplied at creation. |
| `gpus` | Selected devices, each with `model`, `uuid` and worker `device` path. |
| `vnc` | A ready Xvnc desktop's `url`, worker-side `port`, `worker_host` and `ssh_command`; otherwise `None`. |

CPU and memory are configuration, not live utilization or dedicated host
reservations. gVisor rounds memory budgets up to whole MiB; CPU weights and
quotas use its sampled controller. With native Apptainer, `vcpus` selects the
number of eligible host CPUs in the affinity mask. Native memory enforcement
uses one sampled RSS limit equal to the sum of both configured budgets.
See [resource limits](#limits-that-matter).

`gpus=[]` means no GPU is selected, including after the runtime stops.
The runtime's launch record supplies device identity, independently of a model
filter such as `gpu="L40S"`. A GPU remains selected while the sandbox is paused.
Its model is `None` if the worker cannot read the NVIDIA driver metadata.
An older worker or third-party runtime that does not report selected devices
returns `gpus=None` for a running GPU sandbox. Unknown worker fields are also
`None`; the client never substitutes its own hostname for a remote worker.

VNC URLs use `127.0.0.1` **on the worker**. Reading `env.info` does not open a
tunnel. From another machine, run `ssh_command` and keep it running while
using the URL. It forwards the same local port to the worker's VNC port and
uses your configured SSH alias, including that alias's SSH configuration.
If you need a different login or jump host from the viewer's machine, adjust
the command there. If the local port is occupied, choose another local port
in `-L` and use that port in the viewer URL. On the worker itself, open the
URL directly. TigerVNC also accepts `127.0.0.1::PORT` for an explicit TCP port.
VNC authentication still uses the desktop's configured password.

Accessing `env.info` contacts the worker and raises on a lost connection or a
closed handle. It excludes setup payloads, environment variables, and control
tokens. `env.spec` remains the full creation specification; `env.status()`
remains the detailed lifecycle/runtime record. Async callers can fetch that
record with `await env.status.aio()`.

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

Setup installs the base image and game inputs selected by a VR recipe. Gamepad flat mode
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
Built-in templates declare an `installation` name, which local extensions
inherit so setup installs the same underlying software.
The sandbox startup deadline includes guest setup, service and control readiness.
First-use installation of host dependencies happens before that deadline starts.
`keep_on_error=True` retains a failed guest for diagnosis using the exception's
`sandbox_id`; it does not return an environment claimed to be ready.

`env.timings` reports startup durations in seconds. `ready_seconds` is the total
inside the worker; it excludes installation, worker preparation, client transport
and cache publication after readiness. The top-level phases are `admission_seconds`,
`snapshot_seconds`, `runtime_seconds`, `setup_seconds`, `services_seconds` and
`controls_seconds`. They exclude small gaps for record writes. `runtime_launch_seconds`
and `runtime_agent_seconds` detail time already included in `runtime_seconds`;
`desktop_*` entries detail time already included in `controls_seconds`. Do not add those
details to their parent totals. Failed creation records retain completed phases
and elapsed time in the failing phase. Pause/resume does not replace startup timings.

The GNOME template starts on the desktop with its VNC clipboard helper hidden.
Its Xvnc session does not provide GDM screen locking; the template suppresses
GNOME's one-time notice about that expected limitation.
Set `initial_overview = true` under `[capabilities.desktop]` in a custom template
to keep GNOME's initial overview. Cold filesystem restores repeat desktop startup;
pause/resume and memory restores retain the user's current presentation.
The built-in GNOME recipe runs its guest service configuration before systemd,
using `runtime_options.init_command`. This optional argument list replaces
`/sbin/init` for a template with `init = "systemd"`; its wrapper must eventually
exec systemd. It runs only on cold boots, inside the sandbox.

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
volumes remain. By default, the worker also terminates a sandbox when its
creating Python process exits, including normal exit, a crash or a killed
IPython kernel. Use `Sandbox(detached=True)` to let it outlive that process.
Stopping a cell while its kernel remains alive is not a process exit.

`close()` disconnects the handle; it does not remove process ownership.
An owned `with`/`async with` context terminates on exit even with `detached=True`.
A `Sandbox.connect(...)` context is borrowed and only disconnects; connecting
does not transfer ownership or extend the creator's lifetime. CLI `create`
creates detached sandboxes; CLI `run` uses an owned scope.

Local workers identify an owner by its process ID, start time, host boot and PID
namespace. A live local process is retained even if it is suspended. Remote
clients send heartbeats every five seconds over a separate connection shared
by their sandboxes on each worker. After 30 seconds without a heartbeat, the
worker expires ownership. A network outage of that duration can therefore
terminate a remote sandbox even if Python is still alive. Late heartbeats do
not revive expired environments.

Ownership is registered before sandbox creation. A client that dies during
startup cannot leave a default environment running indefinitely. Cleanup runs
on the worker, checks every 250 ms and retries failures. In-flight lifecycle
operations finish or reach a startup check before cleanup takes their lock;
these intervals are detection intervals, not a bound on termination duration.
Owner records survive worker restarts. Cleanup needs a running worker; this
does not add worker failover or protection from allocation/host loss.

TTL counts wall time from readiness, including pauses and disconnected clients,
and applies to detached environments too. It is unset by default. Automatic
cleanup releases the runtime without taking a new checkpoint; saved caches,
snapshots, logs and external volumes remain. Restores receive new ownership
from the creating process rather than inheriting the source's owner or detached
setting. `keep_on_error=True` retains a failed environment while its owner is
alive; use it with `detached=True` to inspect after the creating process exits.
Existing environments with no ownership record retain their earlier lifetime.

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
Starting a worker through a plain SSH target prepares its selected template on
that host. Existing Slurm workers and explicit worker metadata targets use their
prepared installation; run setup on those workers before selecting a new
workload. These targets do not install a runtime on the Python client's host.

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
  experimental; it does not isolate graphics or memory bandwidth. The MPS
  transport requires driver 610.43.02 and `nvidia-cuda-mps-control`; its observer
  also needs host GCC unless the imported runtime includes the matching library.
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

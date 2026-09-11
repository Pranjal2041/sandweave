<div align="center">
  <h1>Sandweave</h1>
  <p>Fast, modular sandboxes for AI agents.</p>
  <a href="https://pypi.org/project/sandweave/"><img src="https://img.shields.io/pypi/v/sandweave?style=for-the-badge&logo=pypi&logoColor=white" alt="PyPI version"></a>
  <a href="https://pranjal2041.github.io/sandweave/"><img src="https://img.shields.io/badge/Docs-Read-0F766E?style=for-the-badge&logo=readthedocs&logoColor=white" alt="Read the docs"></a>
  <a href="#install"><img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11 and newer"></a>
</div>

Sandweave runs Linux sandboxes for agent training and evaluation. Use Python or
the CLI to run code, control a desktop, or interact with a VR game. Sandboxes run
on your own workers without host sudo or KVM.

A template defines the installed software, startup services and controls.
A sandbox is a running instance of that template. You can use a built-in
template, provide a setup script, or write your own template. You can also use
a Docker image as its filesystem base.

## Install

Inside a Python 3.11+ environment on a Linux x86-64 worker (kernel 5.6 or newer):

```bash
uv pip install sandweave
```

You can also use `pip install sandweave`.

Creating a local sandbox installs its template on first use. Each project uses
its own `.sandweave` directory unless you choose another storage path. Setup checks your machine and downloads a
compatible prebuilt runtime from GitHub Releases. If no matching binary is
available, it builds from source. The first installation also downloads the
selected template's software, so it needs internet access.

Storage settings belong to the project where you run Sandweave. A different
project starts with its own installation. Setup does not search your home or
parent directories for runtimes. To share an installation explicitly, set
`SANDWEAVE_HOME` to its storage directory.

To choose storage and prepare a template ahead of time:

```bash
sandweave setup
```

Setup asks what you want to start with and where to store files. The directory
can be empty. It installs the template and checks a temporary sandbox. Downloads,
installed runtimes and caches stay in the selected directory. Other templates
are installed when you first use them.
Setup shows live progress, elapsed time and the latest build output. Downloads
show byte progress when the server provides a size. Full logs are saved in
`logs/setup` inside your selected directory.
See [installation details](https://pranjal2041.github.io/sandweave/installation/).

To check your installation or fix a problem:

```bash
sandweave doctor
```

Doctor shows the checks for your workload. Select a repair in the terminal menu
to apply it, then see the updated results. For scripts and CI, use
`sandweave doctor --check` or `sandweave doctor --json` without interactive fixes.

<a id="agreed-public-api-contract-v1"></a>

## Run commands

```python
from sandweave import Sandbox

with Sandbox() as env:
    result = env.run("python -c 'print(2 + 2)'")
    print(result.stdout)
```

This prints `4`. The default `coding` template provides Python, a shell and
basic tools, with one virtual CPU and 1 GiB of guest memory.
`Sandbox(...)` waits for the template's services and controls to be ready.
Leaving the `with` block terminates the sandbox and discards unsaved state.

By default, a sandbox also terminates when its creating Python process exits,
including a crash or an IPython kernel shutdown. The SDK sends heartbeats
automatically every five seconds; remote ownership expires after ten minutes
without a successful renewal. To keep an environment running after Python
exits, pass `detached=True`:

```python
env = Sandbox(template="gnome", detached=True)
print(env.id)  # Reconnect later with Sandbox.connect(id).
```

Detached environments still respect `ttl` and explicit termination. A `with`
block still terminates a sandbox it creates, including a detached one.

`run` takes one command string, runs it through `/bin/sh -c` inside the sandbox
by default, and waits for completion. Pipes, redirects, variable expansion and
`&&` use the guest shell. The result contains `stdout`, `stderr` and `returncode`.
By default, a nonzero exit returns a result. Pass `check=True` to raise a
`CommandError` instead. Timeouts and sandbox connection failures still raise.

Each call starts a new process. Set command options when you need them:

```python
with Sandbox() as env:
    # Set the working directory and environment for one command.
    result = env.run("echo $MODE", cwd="/workspace", env={"MODE": "eval"})
    print(result.stdout)

    # Read the guest's traceback and exit code.
    result = env.run("python -c 'print(2 / 0)'")
    print(result.stderr)
    print(result.returncode)  # 1

    # Require a command to succeed.
    env.run("python -c 'print(2 + 2)'", check=True)
```

See [command options](https://github.com/Pranjal2041/sandweave/blob/main/notes/sdk-usage.md#commands) for shells, timeouts and terminals.

To stream output, use `exec`:

```python
from sandweave import Sandbox

with Sandbox() as env:
    env.files.upload("./train.py", "/workspace/train.py")
    process = env.exec("python -u /workspace/train.py", timeout=60)
    for line in process.stdout:
        print(line, end="")
    process.wait(check=True)
```

`exec` returns a `Process` with stdin, stdout, stderr, `wait`, `poll` and
`terminate`. Here, `train.py` is your local script; `wait(check=True)` checks its
exit status. File access also includes `write_text`, `read_text`, `download` and
streaming through `env.files.open(...)`.

## Use a Docker image

```python
from sandweave import Sandbox

with Sandbox(image="docker://python:3.12-slim") as env:
    print(env.run("python --version").stdout)
```

Images are downloaded and prepared once, then reused for independent sandboxes.
A custom template can define `image = "docker://python:3.12-slim"` alongside its
setup and services. Passing both `template=` and `image=` overrides the template's
base while retaining its recipe. No Docker daemon is needed.

Commands inherit the image's environment, user, and working directory. Sandweave
starts its command service; the image's `ENTRYPOINT` and `CMD` do not run
automatically. See [image defaults, caching, and supported images](https://pranjal2041.github.io/sandweave/images/).

## Set up a desktop

Create a desktop; its dependencies are installed automatically if needed:

```python
from sandweave import Sandbox
env = Sandbox(template="gnome")
env.setup("./install-chrome-and-myapp.sh")
```

Write `install-chrome-and-myapp.sh` to install your applications. The setup
script runs inside the sandbox. The `gnome` template starts GNOME at 1920×1080 with Xvnc and
provides screenshots, mouse input and keyboard input:

```python
image = env.desktop.screenshot()
env.desktop.mouse.click(400, 300)
env.desktop.keyboard.type("hello")
```

Keyboard input goes to the focused window; mouse clicks use desktop coordinates.
For an agent loop, `env.desktop.step(action)` applies a mouse/keyboard action and returns an
observation with `.image` and timing metadata. It captures after the input server
acknowledges the action; your application may still be processing it.
See the [desktop loop example](https://github.com/Pranjal2041/sandweave/blob/main/notes/sandbox-api-examples.md#4-a-desktop-agent-loop).

To inspect startup time:

```python
print(env.timings)
```

`ready_seconds` measures sandbox startup on the worker. The other entries show
time spent launching the runtime, running setup and waiting for the desktop.
First-use installation and worker preparation happen before this timer starts.
See [startup measurements](https://github.com/Pranjal2041/sandweave/blob/main/notes/gnome-startup-profiling.md) for the breakdown.

## Inspect a sandbox

`env.info` fetches a summary from the worker:

```python
from pprint import pprint

pprint(env.info)
```

It includes the sandbox's ID, name, state, template, runtime, worker hostname,
CPU and memory settings, selected GPUs, and VNC connection details.
For example, a default GNOME sandbox has:

```python
info = env.info
print(info["cpu"])     # {"vcpus": 4, "weight": 100, "quota": None}
print(info["memory"])  # {"guest": "8GiB", "runtime": "1GiB"}
print(info["gpus"])    # [] unless you requested a GPU
print(info["vnc"]["url"])
print(info["vnc"]["password"])
```

CPU and memory values are configured budgets, not current utilization. GPU
entries identify the selected device by model, UUID and device path.

VNC URLs use the worker's loopback address; remote access depends on your setup.
`vnc` is `None` when there is no ready Xvnc desktop.

The CLI provides the same summary:

```bash
sandweave info SANDBOX_ID
```

Replace `SANDBOX_ID` with `env.id` or a unique sandbox name.
See [inspection details](https://github.com/Pranjal2041/sandweave/blob/main/notes/sdk-usage.md#inspect-a-sandbox) for field meanings
and compatibility with older workers.

## Cache and reuse an environment

Continuing with the desktop above:

```python
baseline = env.cache("my-workbench")
env.terminate()

with Sandbox(cache="my-workbench") as env:
    image = env.desktop.screenshot()
```

Caches save filesystem state by default, including installed software and files.
The saved template also supplies startup services and controls, so you can
restore by cache name alone. Processes start fresh, and each restore gets
independent writable state. Shared external volumes keep their own state.

A cache name can be updated. Use the returned reference to keep the same version:

```python
# Reuse this saved version even if the cache name changes.
with Sandbox(cache=baseline) as env:
    image = env.desktop.screenshot()
```

A missing cache raises an error.

To reuse setup automatically, pass a `cache_key`:

```python
with Sandbox(template="gnome", setup="./install-tools.sh",
             cache_key="tools-build") as env:
    image = env.desktop.screenshot()
```

`cache_key` reuses matching preparation or builds a new revision when the
template, script or declared inputs change. `cache` loads saved state.
The setup scripts in these examples are files you provide.

## Choose or write a template

| Template | Includes |
| --- | --- |
| `coding` | Python, shell and file access; the default. |
| `gnome` | GNOME desktop, screenshots, mouse and keyboard controls. |
| `cuda` | Coding environment with one eligible NVIDIA GPU. |
| `docker` | A Docker daemon inside the sandbox. |
| `vr/opensaber` | Open Saber, virtual controllers and paired eye images. |
| `vr/gunspinning` | GunSpinning VR with motion controllers and paired eye images. |
| `games/gunspinning-gamepad` | GunSpinning's flat gamepad mode with desktop output. |

A setup script on its own uses the coding template. A template can also be a
local TOML file or a `Template` object:

```python
with Sandbox(setup="./setup-coding.sh") as env:
    print(env.run("python --version").stdout)

with Sandbox(template="./my-desktop.toml") as env:
    image = env.desktop.screenshot()
```

For example, `my-desktop.toml` can extend the GNOME template:

```toml
extends = "gnome"

[setup]
script = "install-tools.sh"
```

Write `install-tools.sh` to install your applications. See
[writing templates](https://github.com/Pranjal2041/sandweave/blob/main/notes/sdk-usage.md#templates) for startup services and custom
controls. The [point-mass example](https://github.com/Pranjal2041/sandweave/blob/main/examples/pointmass/README.md) shows how to
add controls through an installed Python package.

## Run a pool of sandboxes

```python
from sandweave import Pool

def evaluate(env, source):
    env.files.write_text("/workspace/main.py", source)
    return env.run("python /workspace/main.py", timeout=5)

programs = ["print(1 + 1)", "print(2 + 2)"]

with Pool(template="coding", size=32, warm=8) as pool:
    results = list(pool.map(evaluate, programs))
```

`size` limits the number of sandboxes in use at once. `warm` requests a reserve
of ready sandboxes within that capacity and the worker's available resources.
Entering the pool waits for the initial reserve.

Each task receives independent starting state. After a task, the pool discards
the used sandbox and replaces it from the baseline. The callback runs in your
Python process, and results preserve input order. Use `cache=baseline` for a
prepared environment or `targets=[...]` to distribute tasks across workers.

## Manage workers with Weave

Start a controller with a local worker:

```bash
sandweave cluster start lab
```

Startup prints a dashboard URL, HTTP and SSH addresses, and complete worker join
commands. Open the dashboard link in your browser or copy either join command
onto another machine. Both connection methods are available immediately;
no transport selection or saved connection is needed. Use `--no-worker` to run
only the controller.

Links include authentication, so keep them private. HTTP requires a network route
to the printed host and port; SSH uses your existing SSH login. HTTP is
unencrypted. HTTPS is available when you supply certificates.
`sandweave cluster instructions lab` prints the links again. See the
[connection guide](https://github.com/Pranjal2041/sandweave/blob/main/notes/weave-usage.md#add-workers)
for certificates, custom addresses and resource limits.

Use its name as the target. The controller assigns sandboxes to workers and
maintains the pool's ready reserve:

```python
from sandweave import Pool

with Pool(target="lab", size=8, warm=2) as pool:
    with pool.acquire() as env:
        print(env.run("python --version").stdout)
    pool.update(size=16, warm=4)
```

Reuse image files across workers and prefer placement on the same machine:

```python
with Pool(target="lab", size=8, warm=2,
          shared_cache="/shared/sandweave", affinity="machine") as pool:
    with pool.acquire() as env:
        print(env.run("python --version").stdout)
```

`shared_cache` is a directory on the workers; each sandbox keeps independent
writable state. `affinity="worker"` prefers the same worker instead. Affinity
falls back when capacity is unavailable. See [shared caches and placement](https://pranjal2041.github.io/sandweave/pools/#reuse-images-across-workers).

Add an existing SSH worker with
`sandweave cluster add lab --target ssh://worker-two`. Each worker needs
Sandweave installed. You can also register existing Slurm allocations.

`lab` is a connection name saved in this project's storage. From another machine,
use the complete link printed by the controller. The same link works in Python:

```python
from sandweave import Sandbox

with Sandbox(target="https://master.example:8765") as env:
    print(env.run("python --version").stdout)
```

In that example, replace the address with the printed link, including its
credential fragment for HTTP(S), or supply `SANDWEAVE_TOKEN_FILE` separately.
Append `--cpus 8 --gpus 1` to a printed join command to contribute part of an allocation.

Omit the limits to contribute all CPUs and GPUs available to that worker
process. Workers initiate their connections; clients need only reach the
controller. HTTP and SSH connections are also supported. See the
[connection examples](https://github.com/Pranjal2041/sandweave/blob/main/notes/weave-usage.md#add-workers)
for starting the listener, credentials, TLS certificates and SSH addresses.

Submit a command that can outlive your Python process:

```python
from sandweave import Job

job = Job.submit("python -c 'print(2 + 2)'", target="lab", detached=True)
print(job.id)  # Reconnect with Job.connect(job_id, target="lab").
print(job.result().stdout)
```

Jobs retain their inputs, attempts and results across controller restarts.
Retries are opt-in. Pools support weights, priorities, worker labels and
draining. See the [Weave guide](https://github.com/Pranjal2041/sandweave/blob/main/notes/weave-usage.md) for batch jobs, remote
controllers and lifecycle details.

Sandweave includes the initial Weave implementation. It uses one controller
for one trusted account; automatic machine provisioning, controller failover and
team permissions are later stages of the [design](https://github.com/Pranjal2041/sandweave/blob/main/notes/weave-design.md).

### Monitor your cluster

Open the dashboard for a running controller:

```bash
sandweave dashboard lab
```

See workers, sandboxes, pools, jobs, snapshots, and events in one place. The
dashboard updates automatically and includes resource charts, scheduling delays,
startup timings, and command logs. Select a resource to inspect its state and
related workloads. CPU, memory, and GPU measurements are shown separately from
configured capacity and reservations.

The dashboard is served by the controller and uses a read-only browser session.
It also supports remote HTTP, HTTPS, and SSH cluster targets. No separate frontend
installation is needed. See the [dashboard guide](https://github.com/Pranjal2041/sandweave/blob/main/notes/dashboard.md)
for connections, filters, retention, measurement definitions, and Prometheus.

## Use async calls

```python
from sandweave import Sandbox

async def evaluate_one(source):
    async with await Sandbox.create.aio(template="coding") as env:
        await env.files.write_text.aio("/workspace/main.py", source)
        result = await env.run.aio("python /workspace/main.py", timeout=5)
        return result.stdout
```

Creation, commands, file operations and lifecycle methods have `.aio`
counterparts. Async contexts follow the same cleanup rules as synchronous ones.

## Run a VR agent loop

Run `sandweave setup --template vr/gunspinning` on a worker with an allocated
NVIDIA GPU. For a first installation, setup asks for the game's free
[Linux download](https://demonixis.itch.io/gunspinning-vr).

```python
from sandweave import Sandbox

def play_episode(policy):
    with Sandbox(template="vr/gunspinning", gpu=True) as env:
        observation = env.vr.observe()
        with env.vr.record("./episode", fps=30):
            for _ in range(300):
                action = policy(observation.left, observation.right)
                if action is None:
                    break
                observation = env.vr.step(action)
```

Pass your policy to `play_episode`. It receives left and right images from the
same compositor frame and returns head/controller input, or `None` to end the
loop. The template starts the game and virtual controllers.

Recording saves lossless eye pairs, separate left/right MP4 previews, a
synchronized side-by-side MP4 preview, and timing and dropped-frame metadata.
The MP4 previews use lossy encoding. Video export requires at least two recorded
frames; a policy that ends immediately can leave the recording too short.
`fps=30` requests a capture cadence, not a game frame rate.

`vr.step` returns a capture taken after the runtime acknowledges the input.
Acknowledgement does not guarantee that the game consumed the input or advanced
one simulation tick. See [VR actions and caching](https://github.com/Pranjal2041/sandweave/blob/main/notes/sandbox-api-examples.md#9-a-vr-game-and-agent-loop)
for the action format and offline reuse. Filesystem restores start fresh game
processes; live graphics checkpoints are unsupported.

## Configure a sandbox

```python
from sandweave import Sandbox

# Increase CPU and memory.
with Sandbox(cpu=4, memory="8GiB") as env:
    print(env.run("python --version").stdout)

# Block outgoing network access.
with Sandbox(network="offline") as env:
    print(env.run("python -c 'print(2 + 2)'").stdout)

# Set a five-minute lifetime limit, including time spent paused.
with Sandbox(ttl=300) as env:
    print(env.id)

# Run code with the native Apptainer runtime.
with Sandbox(runtime="apptainer") as env:
    print(env.run("python --version").stdout)
```

For GPU selection, CPU sharing and separate guest/runtime memory budgets:

```python
from sandweave import CPU, Memory, Sandbox

with Sandbox(template="cuda", gpu="L40S",
             cpu=CPU(vcpus=2, weight=200, quota=1.5),
             memory=Memory(guest="4GiB", runtime="1GiB")) as env:
    print(env.run("nvidia-smi").stdout)
```

GPU selection uses hardware already allocated to the worker. Native Apptainer
supports commands and filesystem caches, with fewer isolation and resource
controls than gVisor, the default runtime. See
[resource and runtime limits](https://github.com/Pranjal2041/sandweave/blob/main/notes/sdk-usage.md#limits-that-matter).

Add disk-backed memory with an explicit directory on the worker:

```python
with Sandbox(memory=Memory(guest="4GiB", disk="16GiB",
                           disk_path="/scratch/my-memory")) as env:
    print(env.info["memory"])
```

Applications see 20 GiB. Linux pages colder data to disk within a host RAM cap
of 4 GiB plus the runtime allowance. Every sandbox gets a private random
subdirectory; termination releases its backing storage. This directory is
independent of installation storage and caches. Disk memory requires gVisor,
a supported disk filesystem, and a delegated memory cgroup or a Slurm allocation
with enforced step memory limits. See [disk-backed memory](https://pranjal2041.github.io/sandweave/resources/#disk-backed-memory)
for storage requirements and performance measurements.

To give sandboxes separate proxy exits, pass a proxy URL or a list:

```python
from sandweave import Network

with Sandbox(network=Network(proxy=proxies)) as env:
    print(env.run("curl -s https://api.ipify.org").stdout)
```

Here, `proxies` is your list of proxy URLs. Each sandbox selects one for its
lifetime. Compatible tools receive proxy settings; direct egress is blocked.
See [proxy networking](https://pranjal2041.github.io/sandweave/networking/#use-a-proxy)
for credentials, pools, browser configuration and setup behavior.

Group proxy URLs by region to control their distribution:

```python
from sandweave import Network, Pool, ProxyPolicy

# Each value is your list of authenticated proxy URLs for that region.
proxies = {"uk": uk_proxies, "us": us_proxies}

with Pool(size=8, network=Network(
    proxy=proxies, policy=ProxyPolicy("same_region")
)) as pool:
    with pool.acquire() as env:
        print(env.info["network"])
```

`same_region` selects one supplied region for the pool, then cycles through its
proxies. `same_proxy` shares one selected proxy; `round_robin` cycles through all
eligible proxies. To restrict any policy to a region, use
`ProxyPolicy("random", region="uk")`. The same `Network` object works with a
standalone `Sandbox`, where it selects one eligible proxy. See
[proxy policies](https://pranjal2041.github.io/sandweave/networking/#proxy-policies)
for assignment, retries and snapshots.

## Choose a worker

Sandboxes run locally by default. To use an existing Slurm job:

```python
from sandweave import Sandbox, Slurm

allocation = Slurm.connect("12345")
with Sandbox(target=allocation) as env:
    print(env.run("hostname").stdout)
```

Replace `12345` with your job ID. `Slurm.connect` borrows an allocation; sandbox
cleanup leaves the job running. `Slurm.acquire(...)` creates a new allocation
and its context closes only the job it owns. SSH targets and pools across
workers are also supported. See [placement and pools](https://github.com/Pranjal2041/sandweave/blob/main/notes/sdk-usage.md#placement-and-pools)
for configuration and shared-storage requirements.

## Pause, checkpoint and restore

```python
from sandweave import Sandbox

with Sandbox() as env:
    env.files.write_text("/workspace/note.txt", "saved state")
    env.pause()
    env.resume()
    checkpoint = env.snapshot(state="memory")
    if checkpoint.verify()["status"] != "passed":
        raise RuntimeError("Checkpoint verification failed")

with Sandbox(snapshot=checkpoint) as restored:
    print(restored.files.read_text("/workspace/note.txt"))
```

Memory snapshots preserve supported process and kernel state as well as files.
The example waits for verification and checks the result before restoring the
checkpoint. CUDA-only memory restore is experimental and requires
`experimental_gpu_live=True` at capture and restore; ordinary GPU graphics state
cannot be restored.

| Operation | Effect |
| --- | --- |
| `env.pause()` / `env.resume()` | Suspend or continue the same resident sandbox, retaining memory and VRAM. |
| `env.stop()` | Save a checkpoint, then release the runtime. Returns the checkpoint; a failed save keeps the source alive. |
| `env.terminate()` | Release the runtime and discard unsaved state. Existing caches and external volumes remain. |
| `env.close()` | Disconnect this handle; process ownership and TTL still apply. |
| `with Sandbox(...)` | Create an owned sandbox and terminate it on exit. Save first to retain state. |
| `with Sandbox.connect(id)` | Borrow a handle; leaving the block only disconnects. |

See [saved state and ownership](https://github.com/Pranjal2041/sandweave/blob/main/notes/sdk-usage.md#saved-state-and-ownership) for
snapshot storage, verification and external mounts.

## Use the CLI

```bash
sandweave run --template coding -- "python -c 'print(2 + 2)'"

sandweave create --template gnome --setup ./install-tools.sh --name workbench
sandweave desktop screenshot workbench --output screen.png
sandweave cache save workbench my-workbench
sandweave create --cache my-workbench --name restored

sandweave create --template vr/gunspinning --gpu auto --name gunspin
sandweave vr record gunspin --duration 30 --output ./episode
```

`run` creates a sandbox for one command and terminates it afterward. `create`
uses `detached=True`, leaving the sandbox running until its TTL expires or you
stop or terminate it:

```bash
# Save the sandbox before stopping it.
sandweave stop workbench

# Discard unsaved changes.
sandweave terminate restored
sandweave terminate gunspin
```

Both `run` and `exec` take one quoted command string after `--`.
`--argv -- PROGRAM ARG ...` selects literal arguments without a shell. Command
stdout, stderr and exit codes pass through to your terminal. Python and CLI
operations share the same lifecycle.

## Reference

- [Contributing and releases](https://github.com/Pranjal2041/sandweave/blob/main/CONTRIBUTING.md): development checks and `./deploy`.
- [Usage guide](https://github.com/Pranjal2041/sandweave/blob/main/notes/sdk-usage.md): configuration, templates, extensions and limits.
- [More examples](https://github.com/Pranjal2041/sandweave/blob/main/notes/sandbox-api-examples.md): agent loops, files, pools and Slurm allocation.
- [Test results and measurements](https://github.com/Pranjal2041/sandweave/blob/main/notes/sdk-implementation-progress.md): completed acceptance checks and performance results.
- [API design contract](https://github.com/Pranjal2041/sandweave/blob/main/notes/sandbox-api-proposal.md) and [repository structure](https://github.com/Pranjal2041/sandweave/blob/main/notes/repository-architecture.md).
- [Lab feature inventory](https://github.com/Pranjal2041/sandweave/blob/main/notes/feature-inventory.md) and [runtime reproduction](https://github.com/Pranjal2041/sandweave/blob/main/notes/gvisor-lab-reproduction.md): engine experiments and their limits.

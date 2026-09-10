<div align="center">
  <h1>Sandweave</h1>
  <p>Fast, modular sandboxes for AI agents.</p>
  <a href="https://pypi.org/project/sandweave/"><img src="https://img.shields.io/pypi/v/sandweave?style=for-the-badge&logo=pypi&logoColor=white" alt="PyPI version"></a>
  <a href="https://github.com/Pranjal2041/sandweave/blob/main/notes/sdk-usage.md"><img src="https://img.shields.io/badge/Docs-Read-2563EB?style=for-the-badge&logo=readthedocs&logoColor=white" alt="Read the docs"></a>
  <a href="#install"><img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11 and newer"></a>
</div>

Sandweave runs Linux sandboxes for agent training and evaluation. Use Python or
the CLI to run code, control a desktop, or interact with a VR game. Sandboxes run
on your own workers without host sudo or KVM.

A template defines the installed software, startup services and controls.
A sandbox is a running instance of that template. You can use a built-in
template, provide a setup script, or write your own template.

## Install

Inside a Python 3.11+ environment on a Linux x86-64 worker (kernel 5.6 or newer):

```bash
uv pip install sandweave
```

You can also use `pip install sandweave`.

Creating a local sandbox installs its template on first use. Sandweave reuses
your configured storage directory, or creates `.sandweave` in the current
directory if you have not chosen one. Setup checks your machine and downloads a
compatible prebuilt runtime from GitHub Releases. If no matching binary is
available, it builds from source. The first installation also downloads the
selected template's software, so it needs internet access.

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
See [installation details](https://github.com/Pranjal2041/sandweave/blob/main/notes/sdk-usage.md#install-and-configure).

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
including a crash or an IPython kernel shutdown. Remote workers allow a
30-second heartbeat grace period. To keep an environment running after Python
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

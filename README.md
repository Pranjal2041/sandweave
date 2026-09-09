# Sandweave

Sandweave runs Linux sandboxes for agent training and evaluation. Use Python or
the CLI to run code, control a desktop, or interact with a VR game. Sandboxes run
on your own workers without host sudo or KVM.

A template defines the installed software, startup services and controls.
A sandbox is a running instance of that template. You can use a built-in
template, provide a setup script, or write your own template.

## Install

From this checkout, inside a Python 3.11+ environment on a Linux x86-64 worker:

```bash
uv pip install .
sandweave setup
```

Choose what you want to start with, then choose where Sandweave should store
its files. That directory can be empty. Setup installs the runtime and selected
template, starts a temporary sandbox, checks that it becomes ready, then releases
it. It can reuse an existing installation; otherwise it downloads and builds the
required inputs.
The first build needs internet access and can take a while.

Setup stores downloads, installed runtimes and caches in the directory you choose.
To add a desktop later, run `sandweave setup --template gnome`.
See [installation details](notes/sdk-usage.md#install-and-configure).

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

`run` takes one command string, runs it through `/bin/sh -c` inside the sandbox
by default, and waits for completion. Pipes, redirects, variable expansion and
`&&` use the guest shell. The result contains `stdout`, `stderr` and `returncode`. A nonzero
exit raises unless you pass `check=False`; a timeout raises an error.

Each call starts a new process. Set command options when you need them:

```python
with Sandbox() as env:
    # Set the working directory and environment for one command.
    result = env.run("echo $MODE", cwd="/workspace", env={"MODE": "eval"})
    print(result.stdout)

    # Inspect a failed command instead of raising an exception.
    result = env.run("exit 1", check=False)
    print(result.returncode)
```

See [command options](notes/sdk-usage.md#commands) for shells, timeouts and terminals.

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

Install the desktop template once with `sandweave setup --template gnome`, then:

```python
from sandweave import Sandbox
env = Sandbox(template="gnome")
env.setup("./install-chrome-and-myapp.sh")
```

Write `install-chrome-and-myapp.sh` to install your applications. The setup
script runs inside the sandbox. The `gnome` template starts GNOME with Xvnc and
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
See the [desktop loop example](notes/sandbox-api-examples.md#4-a-desktop-agent-loop).

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
[writing templates](notes/sdk-usage.md#templates) for startup services and custom
controls. The [point-mass example](examples/pointmass/README.md) shows how to
add controls through an installed Python package.

## Run a pool of sandboxes

```python
from sandweave import Pool

def evaluate(env, source):
    env.files.write_text("/workspace/main.py", source)
    return env.run("python /workspace/main.py", timeout=5, check=False)

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
one simulation tick. See [VR actions and caching](notes/sandbox-api-examples.md#9-a-vr-game-and-agent-loop)
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
[resource and runtime limits](notes/sdk-usage.md#limits-that-matter).

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
workers are also supported. See [placement and pools](notes/sdk-usage.md#placement-and-pools)
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
| `env.close()` | Disconnect this client; the sandbox keeps running. |
| `with Sandbox(...)` | Create an owned sandbox and terminate it on exit. Save first to retain state. |
| `with Sandbox.connect(id)` | Borrow a handle; leaving the block only disconnects. |

See [saved state and ownership](notes/sdk-usage.md#saved-state-and-ownership) for
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
leaves the sandbox running until you stop or terminate it:

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

- [Usage guide](notes/sdk-usage.md): configuration, templates, extensions and limits.
- [More examples](notes/sandbox-api-examples.md): agent loops, files, pools and Slurm allocation.
- [Test results and measurements](notes/sdk-implementation-progress.md): completed acceptance checks and performance results.
- [API design contract](notes/sandbox-api-proposal.md) and [repository structure](notes/repository-architecture.md).
- [Lab feature inventory](notes/feature-inventory.md) and [runtime reproduction](notes/gvisor-lab-reproduction.md): engine experiments and their limits.

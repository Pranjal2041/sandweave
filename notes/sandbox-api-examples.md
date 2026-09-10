# Sandweave examples

These examples expand the [README](../README.md#agreed-public-api-contract-v1).
See the [usage guide](sdk-usage.md) for installation and requirements, and
[acceptance results](sdk-implementation-progress.md) for tested behavior.
The robotics example requires a separately implemented extension.

All `setup` files below are scripts you provide. Application installation
depends on those scripts; choosing a template does not install Chrome.
An owned `with Sandbox(...)` scope is ephemeral and discards unsaved state on
exit. Cache/snapshot first when state must survive. Plain handles follow their
creating Python process unless created with `detached=True`; `stop` saves,
whereas `terminate` discards the current running state.

## 1. A coding sandbox

```python
from sandweave import Sandbox

with Sandbox() as env:
    result = env.run("python -c 'print(2 + 2)'")
    print(result.stdout)  # "4\n"
```

No desktop, GPU, cloud account or deployment command is implied. `run` waits
for completion and returns the exit status. One command string runs through
`/bin/sh -c` inside the sandbox by default. Shell quoting, pipes, redirects and
`&&` work there. `exec` and async methods use the same command-string convention.
Advanced literal arguments use `env.run(argv=[...])`; neither form executes
through a shell on the SDK host.

To run generated code and inspect failures as evaluation results:

```python
from sandweave import Sandbox

def evaluate_code(code):
    with Sandbox(template="coding") as env:
        env.files.write_text("/workspace/solution.py", code)
        result = env.run("python /workspace/solution.py",
                         timeout=5)
        return {"returncode": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr}
```

Execution timeout remains a typed infrastructure/control outcome; a nonzero
program exit returns a result by default. Use `check=True` to raise on a nonzero exit.

## 2. Three lines to a custom desktop

```python
from sandweave import Sandbox
env = Sandbox(template="gnome")
env.setup("./install-chrome-and-myapp.sh")
```

The template has already supplied the desktop, input helper and screenshot
interface. The script installs the user's chosen software. Services that must
start on every cold restore are declared in a custom template, rather than
assumed to remain running after a filesystem-only cache.

```python
image = env.desktop.screenshot()
env.desktop.mouse.click(400, 300)
env.desktop.keyboard.press("ctrl+l")
env.desktop.keyboard.type("text for the focused application")
```

These calls operate on the current desktop focus; they do not automatically
select a browser or wait for a particular application's semantic state.

Save the installed environment, then release it:

```python
desktop_ready = env.cache("chrome-workbench-v1")
env.terminate()  # The published cache remains.
```

Later, the template argument is unnecessary:

```python
from sandweave import Sandbox

with Sandbox(cache="chrome-workbench-v1") as env:
    image = env.desktop.screenshot()
```

For an exact reproducible baseline, pass the returned immutable
`desktop_ready` reference instead of resolving the mutable name again.

## 3. A setup script, template file, or cached preparation

```python
from sandweave import Sandbox

with Sandbox(setup="./setup-coding.sh") as env:
    print(env.run("python --version").stdout)

with Sandbox(template="./my-desktop.toml") as env:
    image = env.desktop.screenshot()
```

Avoid repeating the same preparation across calls:

```python
from sandweave import Sandbox

with Sandbox(template="gnome", setup="./install-tools.sh",
             cache_key="tools-build-v1") as env:
    print(env.id)
```

First use prepares and publishes; a matching later use restores. Changing a
script or declared input changes the preparation fingerprint. A subsequent
`Sandbox(cache="tools-build-v1")` can use the published revision without the
original recipe arguments. `cache_key` is for preparing a recipe; `cache` is
for restoring saved state.

## 4. A desktop agent loop

```python
from sandweave import Sandbox

def desktop_episode(policy, max_actions=100):
    with Sandbox(cache="chrome-workbench-v1") as env:
        image = env.desktop.screenshot()
        for _ in range(max_actions):
            action = policy(image)  # Existing mouse/keyboard dictionary schema.
            if action is None:
                break
            observation = env.desktop.step(action)
            image = observation.image
```

The policy runs in the caller's process and can use any model/training stack.
`step` captures after the X-server input fence; it does not promise that the
application has completed a navigation or repaint. A task can add its own
observable readiness condition.

## 5. Many coding environments

```python
from sandweave import Pool

def evaluate(env, source):
    env.files.write_text("/workspace/main.py", source)
    return env.run("python /workspace/main.py",
                   timeout=5, check=False).stdout

tasks = ["print(1 + 1)", "print(2 + 2)", "print(3 + 3)"]

with Pool(template="coding", size=8, warm=2) as pool:
    results = list(pool.map(evaluate, tasks))
```

The callback runs on the caller side with a leased sandbox. At most eight
leases are active; two idle-ready environments are the requested reserve.
Every task receives independent state. The pool disposes used sandboxes and
refills from the baseline, rather than reusing a previous task's mutable guest.

For installed project dependencies, prepare once, then pin that baseline:

```python
from sandweave import Pool, Sandbox

with Sandbox(setup="./setup-project.sh") as builder:
    baseline = builder.cache("project-ready-v1")

with Pool(cache=baseline, size=32, warm=8) as pool:
    results = list(pool.map(evaluate, tasks))
```

Pool sizing is bounded by actual admitted resources. An exhausted pool does not
turn queued work into imaginary available CPU/GPU capacity.

## 6. Native async calls

```python
import asyncio
from sandweave import Sandbox

async def evaluate_one(source):
    async with await Sandbox.create.aio(template="coding") as env:
        await env.files.write_text.aio("/workspace/main.py", source)
        result = await env.run.aio("python /workspace/main.py", timeout=5)
        return result.stdout

async def main():
    return await asyncio.gather(
        evaluate_one("print(1 + 1)"),
        evaluate_one("print(2 + 2)"),
    )
```

The async context has the same ownership semantics. Large training workloads
use a bounded pool rather than submitting an unbounded `gather`.

## 7. Process streams and file transfer

```python
from sandweave import Sandbox

with Sandbox() as env:
    env.files.upload("./script.py", "/workspace/script.py")
    process = env.exec("python -u /workspace/script.py", timeout=60)
    for line in process.stdout:
        print(line, end="")
    process.wait(check=True)
    env.files.download("/workspace/results.json", "./results.json")
```

Here the user-provided script is responsible for writing `results.json`.
`exec` also supports stdin, stderr, binary streams and a PTY. With arbitrary
large output, the transport drains/spools both streams within declared limits.

## 8. CPU sharing, memory and GPU selection

```python
from sandweave import CPU, Memory, Sandbox

with Sandbox(template="cuda", gpu="L40S",
             cpu=CPU(vcpus=2, weight=200, quota=1.5),
             memory=Memory(guest="4GiB", runtime="1GiB")) as env:
    print(env.run("nvidia-smi").stdout)
```

CPU weights are local sharing weights, not reservations across the cluster.
The memory fields expose guest and runtime budgets separately. This example
selects an already allocated GPU; it does not silently submit a Slurm job.

For an explicit native Apptainer code path:

```python
from sandweave import Sandbox

with Sandbox(template="coding", runtime="apptainer") as env:
    print(env.run("python -c 'print(2 + 2)'").stdout)
    print(env.status()["runtime_status"]["runtime"])
```

The status includes the runtime's isolation, resource controls and supported
snapshot types. Native Apptainer uses the host kernel and network; CPU weights
and quotas, filtered networking and memory snapshots are unavailable.

## 9. A VR game and agent loop

```python
from sandweave import Sandbox

def vr_episode(policy, max_actions=300):
    with Sandbox(template="vr/gunspinning", gpu=True) as env:
        observation = env.vr.observe()
        with env.vr.record("./gunspinning-demo", fps=30):
            for _ in range(max_actions):
                action = policy(observation.left, observation.right)
                if action is None:
                    break
                observation = env.vr.step(action)
```

The template packages game installation/staging, startup, Monado/xrizer,
virtual controls and stereo I/O. Initial asset acquisition can require network
access; an already prepared offline-capable template/cache does not. `gpu=True`
selects compatible available hardware rather than forcing L40S on every user.

An action retains the existing concrete representation:

```python
action = {
    "head": {"position": [0.0, 1.6, 0.0],
             "orientation": [0.0, 0.0, 0.0, 1.0]},
    "right": {"trigger_value": 1.0, "trigger_click": True},
}
```

Omitted pose/control fields persist; a later action releases a held trigger.
The returned eyes are paired compositor views. `step` captures after the runtime
acknowledges input; it does not guarantee game consumption or a fixed number of
simulation ticks.
Recording saves lossless eye pairs and exports lossy left, right and side-by-side
MP4 previews with timing and drop counts. Video export needs FFmpeg with
`libx264` on the Python client's `PATH` and at least two recorded frames.

For repeated offline runs, prepare a filesystem cache once:

```python
from sandweave import Sandbox

with Sandbox(template="vr/gunspinning", gpu=True) as env:
    ready = env.cache("gunspinning-ready", state="filesystem")

with Sandbox(cache=ready, gpu=True, network="offline") as env:
    observation = env.vr.observe()
```

This boots fresh game processes; it does not resume a cached live graphics
context or preserve the exact in-game moment.

## 10. Pause, checkpoint and restore

```python
from sandweave import Sandbox

with Sandbox(template="coding") as env:
    env.files.write_text("/workspace/note.txt", "saved state")
    env.pause()
    env.resume()
    checkpoint = env.snapshot(state="memory")

with Sandbox(snapshot=checkpoint) as restored:
    print(restored.files.read_text("/workspace/note.txt"))
```

CPU memory snapshots also preserve supported live process/kernel state. This
small example avoids claiming that arbitrary externally attached exec streams
are already restorable. Active attachment support is an adapter acceptance gate.

For an explicitly managed desktop:

```python
from sandweave import Sandbox

env = Sandbox(template="gnome")
saved = env.stop()  # Save before release; failure keeps the source alive.
env = Sandbox(snapshot=saved, detached=True)
env.close()  # The restored desktop also survives this Python process exiting.
```

## 11. Existing workers and explicit Slurm acquisition

Configured target names can refer to separate SSH-accessible Slurm allocations.
The current Slurm adapter requires shared access to SDK metadata and assets;
see [placement configuration](sdk-usage.md#placement-and-pools).

```python
from sandweave import Pool

with Pool(cache="project-ready-v1", size=32, warm=8,
          targets=["training-job-a", "training-job-b"]) as pool:
    results = list(pool.map(evaluate, tasks))
```

Acquiring a new job is a separate explicit action, using the site's configured
account and scheduler settings:

```python
from sandweave import Sandbox, Slurm

with Slurm.acquire(gpu="L40S", cpus=8, memory="64GiB",
                   partition="preempt", qos="preempt_qos",
                   walltime="3h") as allocation:
    with Sandbox(template="vr/opensaber", target=allocation) as env:
        observation = env.vr.observe()
```

The VR template requests its GPU requirement. An existing-job target is never
cancelled by sandbox cleanup. This newly acquired allocation is owned by its
explicit context. Scheduler queue time is reported separately from sandbox
creation, and no automatic preemption retry is implied.

## 12. A domain extension

```python
from sandweave import Sandbox

def robotics_episode(policy):
    with Sandbox(template="./robotics.toml", gpu=True) as env:
        robot = env.capability("robotics")
        observation = robot.reset(seed=42)
        for _ in range(100):
            action = policy(observation)
            observation = robot.step(action)
```

This is an extension-contract illustration. A separately installed robotics
plugin/template must actually implement and advertise `reset`/`step`, camera
observations and any simulator timing/reward semantics. It is not a claim that
RoboCasa or a generic robotics adapter already runs in this lab. Desktop, VR
and robotics use their own interaction schemas above the same sandbox lifecycle.

## 13. CLI equivalents

```bash
# One ephemeral coding sandbox, with the command's stdout and exit code.
sandweave run --template coding -- "python -c 'print(2 + 2)'"

# A persistent desktop: template supplies the desktop action capability.
sandweave create --template gnome --setup ./install-tools.sh --name workbench
sandweave desktop screenshot workbench --output screen.png
sandweave desktop action workbench --input '{"mouse":{"left_click":[400,300]}}'

# Save a reusable filesystem baseline; restore with its capabilities.
sandweave cache save workbench chrome-workbench-v1 --state filesystem
sandweave create --cache chrome-workbench-v1 --name workbench-copy

# Explicit lifecycle; stop saves, terminate discards the current running state.
sandweave pause workbench
sandweave resume workbench
sandweave stop workbench
sandweave inspect workbench-copy

# Both-eye VR recording.
sandweave create --template vr/gunspinning --gpu auto --name gunspin
sandweave vr record gunspin --duration 30 --output ./gunspinning-demo

# A bounded prestarted pool.
sandweave pool create --cache project-ready-v1 --size 32 --warm 8 --name coding-pool
```

Names resolve within a configured workspace; commands also accept immutable
sandbox IDs. Persistent examples deliberately remain available until explicitly
stopped/terminated. File operations, shell access, snapshots and target/allocation
inspection follow the same Python contract rather than adding a separate set of
runtime semantics.

`run`/`exec` take one quoted command string after `--`, interpreted by the guest
shell. An explicit `--argv -- PROGRAM ARG ...` selects direct execution; the CLI
does not guess between forms or join multiple arguments into a shell string.

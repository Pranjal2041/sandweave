# Benchmarks

A benchmark supplies a task's instructions, a clean sandbox and an evaluator.
It uses a [pool](pools.md) to prepare and reuse the starting filesystem. Each
attempt gets its own writable state.

Available in the `0.2.20rc4` preview:

```bash
uv pip install 'sandweave[benchmarks]==0.2.20rc4'
```

## Run tasks

Create a benchmark once, then pull a task whenever your client needs a sandbox:

```python
from sandweave import Benchmark

with Benchmark("osworld-energy50-representative", capacity=8) as bench:
    task = bench.next(timeout=60)
    with task as env:
        run_agent(env, task.instruction)
        result = task.evaluate()
```

`bench.next()` returns after acquiring a sandbox and completing task setup.
`next(bench)` is equivalent without a timeout. When every slot is leased, the
call waits for one to be released. A positive timeout limits checkout waiting,
including time queued behind other async acquisitions; it excludes initial pool
preparation and task setup. A timed-out checkout leaves the task available for
another pull. Exhaustion raises `StopIteration`.

Your client decides when to evaluate and what to do with the result. It also
controls concurrency. Pass `target=cluster_url`, using the complete join URL
printed by `sandweave cluster start`, to place sandboxes through Weave. Without
a target, they run on the local worker.

Outside a task context, access `task.env` and release the lease explicitly:

```python
task = bench.next()
try:
    run_agent(task.env, task.instruction)
    result = task.evaluate()
finally:
    task.close()
```

Closing `task.env` releases the same lease. Repeated closes are safe. Closing the
benchmark cancels pending acquisitions and closes its remaining task leases;
running setup and evaluator calls finish before their environments are released.

Sequential iteration is also supported:

```python
from sandweave import Benchmark

with Benchmark("osworld-energy50-representative", capacity=4) as bench:
    for task in bench:
        with task as env:
            run_agent(env, task.instruction)
            result = task.evaluate()
            print(task.id, result.score, result.passed)
```

Provide `run_agent(env, instruction)` using your model and agent loop. For
OSWorld, the observation is `env.desktop.screenshot()`, a PIL image. Actions use
the usual desktop controls:

```python
image = env.desktop.screenshot()
env.desktop.mouse.click(400, 300)
env.desktop.keyboard.press("ctrl+l")
env.desktop.keyboard.type("https://example.com")
env.desktop.keyboard.press("Return")
```

`env.desktop.step(action)` also accepts a mouse/keyboard mapping and returns an
observation with `.image`. OSWorld records these actions for its evaluator.
Use `env.desktop.action("FAIL")` when an agent declares a task infeasible;
`"DONE"` records ordinary completion. Declaring completion does not award a pass.

Evaluate while the task is leased. Leaving its context releases the sandbox even if setup,
the agent, or evaluation raises. Setup and adapter failures raise exceptions.
The canonical evaluator retains its upstream handling of missing results and
getter failures, which can return zero.

## Run agents concurrently

Multiple threads in your client can call `bench.next()` on the same benchmark.
Each gets a different task; at most `capacity` task sandboxes are leased at once.
An async client pulls with `await bench.next.aio(timeout=60)`:

```python
async def episode(bench):
    task = await bench.next.aio(timeout=60)
    async with task as env:
        await run_agent_async(env, task.instruction)
        return await task.evaluate.aio()
```

Async exhaustion raises `StopAsyncIteration`. Cancelling a pull releases any
acquired sandbox and leaves that task available. If setup is already running,
cleanup waits for it to finish. Capacity waiters use a separate bounded executor
so they cannot occupy the threads used by cleanup or other SDK operations.

For clients that prefer callbacks, `map` performs acquisition and evaluation:

```python
with Benchmark("osworld-energy50-representative", capacity=4) as bench:
    for result in bench.map(run_agent):
        print(result.task_id, result.score)
```

`map` runs at most `capacity` callbacks at once and yields evaluations in task
order. Callbacks run in your Python process. Set `return_exceptions=True` to
receive exceptions alongside successful evaluations instead of stopping on the
first failed result. Worker resource admission still applies.

Async agents use the same API:

```python
async with Benchmark("osworld-energy50-representative", capacity=4) as bench:
    async for result in bench.map.aio(run_agent_async):
        print(result.task_id, result.score)
```

For an individual retry, use `bench.task(task_id)` as a context manager.
`bench.tasks` lists task specifications. `bench.results` contains the latest
completed evaluation for each task, in benchmark order.

## Harbor

Use a Harbor dataset name or an existing Harbor task directory. The adapter reads
the original task definitions and runs their verifier through Harbor 0.23.0.
It requires Python 3.12 or newer:

```bash
uv pip install 'sandweave[harbor]==0.2.20rc4'
```

```python
from sandweave import Benchmark

bench = Benchmark("harbor", source="terminal-bench@2.0", capacity=8)
task = bench.next()
try:
    env = task.env
    instruction = task.instruction
    run_agent(env, instruction)
    result = task.evaluate()
    print(result.rewards)
finally:
    task.close()
    bench.close()
```

Pass `target=cluster_url` to use Weave. `source="./my-task"` loads one task;
`source="./my-dataset"` loads its immediate task subdirectories. Downloaded tasks
and trial logs use the selected Sandweave data directory. Pass `output="./results"`
to choose another location for trial logs and artifacts.

Each task can use a different image. `capacity` limits active task attempts across
all images, and `preload` bounds the ready reserve within that capacity. Images
are pinned to a digest once per benchmark. Matching environments share a prepared
filesystem; every attempt receives fresh writable state. Warm tasks do not use
their agent timeout until checkout.

The client uses `env.run`, `env.exec` and `env.files` as usual. Commands inherit
the task's shell, working directory, user and environment. Grading tests are
uploaded only when verification begins. Harbor collects configured artifacts,
runs shared or separate verifiers, and writes its trial results and logs.

`result.rewards` preserves the verifier's named metrics. It does not convert them
to a percentage or invent a pass threshold; `score` and `passed` are `None` for
Harbor. `result.feedback` gives the trial directory. Infrastructure and verifier
errors raise exceptions instead of returning a zero reward.

Evaluation completes the current Harbor agent phase. On the final step, Harbor
stops the environment after collecting its outputs. Call `task.close()` to return
the task's capacity. Repeating `evaluate()` for the same step returns its saved
evaluation.

For a multi-step task, evaluate each step before asking for the next instruction:

```python
task = bench.next()
try:
    while True:
        run_agent(task.env, task.instruction)
        print(task.evaluate().rewards)
        try:
            task.next_step()
        except StopIteration:
            break
finally:
    task.close()
```

The sandbox persists between steps. Harbor's step setup, verification and early
stop thresholds still apply. Intermediate evaluations report the current step's
rewards; the final evaluation reports Harbor's configured aggregate rewards.
Async clients use `.aio()` on these methods;
async exhaustion raises `StopAsyncIteration`.

The provider currently accepts **prebuilt public Linux amd64 images** with public
or offline networking. Dockerfile-only builds, Compose service groups, private
image authentication, Windows, network allowlists and network changes between
phases need additional runtime support. Unsupported requirements are rejected.
CPU values are reservations rather than hard quotas; guest memory is bounded,
and runtime overhead is admitted separately. Storage quotas are not enforced.
MCP/skills-driven agents and simulated-user bridges are not part of this client
pull API. A task image cannot supply missing kernel features or host devices.

To use Harbor's own agents with the same environment provider:

```bash
harbor run -d terminal-bench@2.0 --env sandweave.benchmarks.harbor.provider:SandweaveEnvironment
```

The [acceptance record](https://github.com/Pranjal2041/sandweave/blob/feat/osworld-benchmarks/notes/harbor-adapter.md)
lists the tasks and lifecycle checks exercised. Existing OSWorld behavior is unchanged.

## OSWorld setup

Both `"osworld"` and `"osworld-energy50-representative"` use the desktop recipe
and evaluation integration from the private
[`Pranjal2041/cua-speed-run`](https://github.com/Pranjal2041/cua-speed-run)
repository at commit `681f8dbc695ff3a7e3af2f532bec982725818211`. The first name
selects the pinned OSWorld task list; the second selects its 50-task representative
split, including the split's setup patches and source checksums.

Authenticate `gh` with an account that can read that repository. Sandweave fetches
the pinned source automatically. Alternatively, pass an unchanged local checkout:

```python
bench = Benchmark(
    "osworld-energy50-representative",
    source="/path/to/cua-speed-run",
    capacity=4,
)
```

The checkout must be at the commit above. Sandweave reads it without editing it.
Private source files are not included in Sandweave's package or runtime release.
The canonical evaluator runs in separate processes on the client. Its dependencies
are prepared automatically if a compatible interpreter is not already available.
The evaluator client needs the Linux `file` command for the upstream file-type
checks; this host utility is not installed by pip.
Verifier code and task answers stay outside the agent sandbox.

The first worker downloads the checksum-pinned official OSWorld QCOW2 image,
reads its filesystem without mounting it on the host, and applies the reference
desktop recipe inside the sandbox. No pre-exported filesystem archive, Modal
account, Docker daemon, host sudo or KVM is required.

Allow roughly 70 GiB of temporary disk space for the initial download, disk
export and image preparation, plus space for task downloads and snapshots.
Prepared images are reused. Set `SANDWEAVE_HOME` to choose storage explicitly;
otherwise files stay in the project's `.sandweave` directory.

Default resources per sandbox are four vCPUs, 16 GiB of guest memory and 1 GiB of
runtime memory. `capacity=4` therefore requires workers with enough admission
capacity for four such environments. `preload` controls the initial ready reserve
and defaults to `capacity`. Ordinary pool options pass through:

```python
with Benchmark("osworld-energy50-representative", capacity=8, preload=2,
               target="lab", shared_cache="/shared/sandweave") as bench:
    for result in bench.map(run_agent):
        print(result)
```

Install the preview SDK on the client, controller and participating workers.
Changing resource settings changes the benchmark conditions; it does not change
the task instructions or scoring rules.

## Desktop and evaluation parity

The integration uses the original Ubuntu 22.04 filesystem, its installed
applications, GDM autologin for `user` (UID 1000), GNOME Ubuntu session and dock,
and dummy Xorg at 1920×1080. Screenshots include the cursor. Keyboard input uses
the reference implementation; mouse input uses matching Xdotool commands.
The reference's 20-second desktop settle and five-second action settle remain
in place. They contribute to elapsed time.

Task setup and scoring use the pinned upstream hooks and canonical OSWorld
evaluator. After GUI launch and document-open commands, Sandweave waits for the
matching application window before continuing setup or exposing the first
observation. Original application prompts, such as a color-profile choice, are
left for the agent to answer. OSWorld scores are returned on the reference's
0–100 scale; a pass requires canonical reward 1. The representative split pins all 50 task JSON
checksums. A screenshot/setup audit is not an agent accuracy measurement.

The OSWorld template enables the engine support used by the original Avahi,
console palette and sysctl services. It does not modify the shared base image or
mask these units. These settings also survive live snapshots.

Full VM parity is not claimed. The reference selects Modal's VM runtime;
Sandweave uses its no-KVM gVisor engine. The tested desktop and verifier paths
work, but Linux hardware, real-time scheduling and some service behavior differ.
The [acceptance record](https://github.com/Pranjal2041/sandweave/blob/feat/osworld-benchmarks/notes/osworld-acceptance.md)
identifies those differences and the tasks exercised.

## Add an integration

An integration provides `tasks`, `prepare()`, `setup(env, task)` and
`evaluate(env, task)`. `prepare()` returns sandbox options for the pool.
Evaluators return `Evaluation`; optional `close()` releases integration-owned
resources. Keep task-specific setup and evaluation with that integration.

```python
from sandweave import Benchmark
from sandweave.benchmarks import Evaluation, TaskSpec

class Arithmetic:
    tasks = [TaskSpec("addition", "Write 2 + 2 to /workspace/answer.txt.")]

    def prepare(self):
        return {"template": "coding"}

    def setup(self, env, task):
        pass

    def evaluate(self, env, task):
        result = env.run("cat /workspace/answer.txt")
        passed = result.returncode == 0 and result.stdout.strip() == "4"
        return Evaluation(task.id, 100.0 if passed else 0.0, passed)

with Benchmark(Arithmetic(), capacity=1) as bench:
    for result in bench.map(run_agent):
        print(result)
```

Installed packages can register a factory under `sandweave.benchmarks.v1`.
The entry-point name becomes the first `Benchmark` argument; its factory
receives `source=`. Duplicate names are rejected.

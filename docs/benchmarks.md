# Benchmarks

A benchmark supplies a task's instructions, a clean sandbox and an evaluator.
It uses a [pool](pools.md) to prepare and reuse the starting filesystem. Each
attempt gets its own writable state.

Available in the `0.2.20rc1` preview:

```bash
uv pip install 'sandweave[benchmarks]==0.2.20rc1'
```

## Run tasks

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

Evaluate inside the task context. Leaving it releases the sandbox even if setup,
the agent, or evaluation raises. Setup and adapter failures raise exceptions.
The canonical evaluator retains its upstream handling of missing results and
getter failures, which can return zero.

## Run agents concurrently

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

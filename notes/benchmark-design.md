# Benchmarks

A benchmark leases a clean sandbox, prepares a task, and evaluates the agent's
work. A pool owns the sandboxes; the benchmark owns task instructions, setup,
and scoring. Capacity limits concurrent leases across the whole benchmark.

```python
from sandweave import Benchmark

with Benchmark("osworld-energy50-representative", capacity=4) as bench:
    for task in bench:
        with task as env:
            run_agent(env, task.instruction)
            print(task.evaluate())
```

Iteration is sequential. To run agents concurrently, `bench.map(run_agent)`
calls `run_agent(env, instruction)` for at most `capacity` tasks at once and
returns their evaluations in task order. Exceptions remain exceptions: setup
or verifier failures must not become an agent score of zero. A used sandbox
is discarded on exit, including after setup, agent, or evaluation failures.

The integration contract is a task list, sandbox options, and three methods:
`prepare()` returns the base sandbox options; `setup(env, task)` prepares one
task; `evaluate(env, task)` returns its result. Built-ins and installed entry
points use the same contract. Benchmark-specific code stays together.

## OSWorld parity target

Reference repository: `Pranjal2041/cua-speed-run`, commit
`681f8dbc695ff3a7e3af2f532bec982725818211`. The source checkout is read-only.
Private reference files are dependencies, not files to vendor into this public
repository. The 50-task manifest pins membership, order, and each source JSON.

The reference's `modal-native` backend explicitly uses Modal `vm_runtime=True`.
It boots the original Ubuntu 22.04 root filesystem's systemd, GDM autologin,
Ubuntu GNOME session and dock, and dummy Xorg at 1920×1080. It does not use
default Modal gVisor: its documentation records that runtime rejecting unshare.
Sandweave must qualify this desktop on its own no-KVM engine.

Use the original, checksum-pinned OSWorld image and the reference's exact delta,
task setup, and evaluator. Keep evaluator source and task answers outside the
agent sandbox. Do not substitute Sandweave's existing `ga`/Xvnc GNOME template.
The guest account is `user` (UID 1000), DISPLAY is `:0`, and the session bus and
Xauthority belong to GDM. Record any engine-required changes explicitly and
check their effect on services, applications, actions and observations.

Acceptance requires actual desktop screenshots, negative evaluator results,
and successful evaluations after completing several representative tasks with
keyboard and mouse. Source agreement alone does not establish runtime parity.

The implementation is in `src/sandweave/benchmarks`; the desktop recipe and
controls are in `src/sandweave/templates/osworld`. The [usage guide](../docs/benchmarks.md)
defines the client API. [Acceptance and remaining service differences](osworld-acceptance.md)
record what has been exercised against the reference.

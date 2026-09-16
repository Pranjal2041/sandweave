# Harbor adapter

Implemented 2026-09-16 on `feat/osworld-benchmarks`, targeting Harbor 0.23.0
and Sandweave 0.2.20rc4. This continues the decisions in
[harbor-integration.md](harbor-integration.md).

## Contract

```python
bench = Benchmark("harbor", source="terminal-bench@2.0", capacity=8)
task = bench.next()
env, instruction = task.env, task.instruction
# Run the client's agent.
result = task.evaluate()
task.close()
bench.close()
```

`source` also accepts a local task or a directory of tasks. Harbor loads the
definitions. Sandweave pauses Harbor's original `Trial` at its agent boundary,
returns the sandbox to the caller, and resumes the trial when the caller asks
for evaluation. Harbor retains responsibility for task setup, timeouts, artifact
collection, verifier invocation, rewards, step progression and result files.
Neither the upstream task nor Harbor's installed source is modified.

The integration consists of a loader, an environment provider, and the suspended
trial runner in `src/sandweave/benchmarks/harbor`. Benchmark integrations may now
provide their own task-aware pool factory. The existing single-baseline path
and OSWorld evaluator remain intact. `Evaluation` additionally carries native
named rewards; Harbor does not impose a universal score or pass threshold.

## Capacity and preparation

The task runner owns one capacity budget and one ready reserve for the whole
benchmark. Each distinct environment definition has a lazy filesystem pool with
no independent warm reserve. Image references are resolved once to immutable
digests per benchmark, and matching pool preparation shares one future. Task
inputs and verifier files are copied after checkout, so they cannot contaminate
the shared image baseline. Used sandboxes are discarded.

Preparation and checkout waits have a separate bounded executor. They do not
consume the threads needed by command transfers or cleanup. Waiting callers
are cancellable; cancelled pulls are put back on the benchmark cursor. Pending
task setup and cleanup are drained before shutdown. Multi-step tasks retain the
same agent sandbox, while separate verifier environments use separate leases.
Capacity counts task attempts; a multi-step task may temporarily have an agent
and a separate verifier environment, both subject to normal worker admission.

The agent's time budget starts after the caller claims the ready phase. Waiting
in the warm reserve or between steps does not consume that budget. Harbor 0.23
leaves shared-verifier files behind between steps; the agent-boundary hook clears
Harbor's reserved tests and verifier-log directories before handing out the next
step. It does not alter task files, grading commands or scores.

The final evaluation completes Harbor's trial and stops its environment. The
task reservation lasts until `task.close()`. Repeated evaluation of the same
phase returns the recorded result; the last phase returns Harbor's configured
aggregate reward. The original trial result, per-step results, verifier logs and
artifacts are retained under the selected output directory.

## Runtime boundaries

The initial provider uses prebuilt public Linux amd64 images and direct gVisor
sandboxes. It does not nest Docker. Compose groups, Dockerfile builds, private
registry authentication, Windows, dynamic networking and hostname allowlists
are not provided by this adapter. Unsupported task requirements fail during
validation. The current client-pull agent does not implement MCP, skills or
simulated-user bridges. These require additional integration, not per-benchmark
name checks or a replacement verifier.

CPU values reserve capacity; they are not advertised as hard quotas. Guest
memory is bounded and its runtime overhead is admitted separately. Storage
quotas are not enforced. An image cannot provide missing kernel features or
devices. Loading a dataset does not establish that every workload runs.

## Acceptance

Tests use disposable environments in allocation 10450695 on `babel-u9-24`.
The engine and existing user desktops were not changed.

- Loaded all 89 definitions from `terminal-bench@2.0` through Harbor's registry.
- Ran the unchanged `openssl-selfsigned-cert` image, solution and verifier:
  the solution earned `{"reward": 1.0}`; a pristine unsolved retry earned
  `{"reward": 0.0}`. No task or verifier files were edited.
- Ran the same original task through Harbor's CLI with the external Sandweave
  provider and its oracle agent: one trial, no exceptions, mean reward 1.0.
- Exercised native named rewards, hidden grading inputs, repeated evaluation,
  multi-step guest continuity, explicit advancement and grading-file cleanup.
- Exercised two different images under one four-task capacity, including an
  HTTP controller with two disposable workers. Checkouts timed out without
  consuming tasks; images were prepared once per definition; pools closed.
- Exercised 64 async capacity waiters, cancellation, retry ordering and unrelated
  executor progress.
- Transferred executable files, in-tree symlinks and empty directories in both
  directions. Retrieved 2 MiB command output without the inline-result truncation.
- Exercised a separate verifier image and verified that an agent-private file
  was absent there.
- Ran the existing host regression suite: 493 passed, 5 skipped before packaging.
  The release receipt records the clean installed-wheel checks.
- Ran all four existing live benchmark-pull regressions, including 25 tasks at
  eight simultaneous leases locally and through HTTP, plus async cancellation.
- Ran both synchronous and asynchronous mapping over multi-step Harbor tasks.
- Built the documentation and exercised browser navigation and copy buttons.
  Opened and inspected the Harbor section at desktop and mobile widths.

Authored regression coverage is in `tests/test_harbor.py` and
`tests/integration/test_harbor_live.py`. The optional original-task acceptance
uses `SANDWEAVE_HARBOR_TASK` to select an unchanged downloaded task; it does not
bundle benchmark data or oracle solutions into the public package.

Local evidence is under `/scratch/pranjala/sw-harbor-20260916`, with original-task
trial records under the configured Sandweave home's `benchmarks/harbor/trials`.
The native Harbor CLI receipt is
`native-jobs/2026-09-16__02-10-39/result.json` under that evidence directory.
Release receipts remain under `runs/deploy` in this checkout.

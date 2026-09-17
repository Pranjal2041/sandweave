# Benchmark pull API acceptance

Version: `0.2.20rc3`. The client calls `next(bench)` or
`bench.next(timeout=60)` and receives a prepared task. `task.env`, its context
manager and `task.close()` all refer to one lease. Closing the environment
releases that lease too. Evaluation remains a client decision.

The shared cursor assigns each task once across concurrent pullers. Capacity
timeouts and cancelled pulls return their task to the cursor. Setup failures
release capacity and propagate; an explicit `bench.task(id)` can retry them.
Existing lazy iteration and callback mapping retain their independent traversals.

Validation on the disposable Babel allocation:

- Host suite: 489 passed, three skipped, 140 integration/GPU cases deselected.
- Benchmark tests: 29 passed, including 64 concurrent requests, capacity
  timeouts, repeated cancellation, racing closes and evaluator draining.
- Live local and HTTP Weave pulls: 25 distinct task environments each, eight
  simultaneous leases, 25 successful arithmetic evaluations each. The HTTP run
  used two workers. Unrelated commands continued throughout. All leases released.
  Total test workloads took 27.14 seconds locally and 39.96 seconds over HTTP;
  these include startup and evaluation, not just acquisition latency.
- Live async local and HTTP tests: 64 waiting callers with capacity two. Queued
  timeouts, cancellation, environment closure and unrelated executor work all
  completed. Cancelled tasks remained available, and both pools closed.
- Documentation: strict build and browser checks passed across 23 pages,
  101 Python examples and 1,706 internal links, including mobile navigation.

The same five Energy50 tasks from [the prior setup audit](osworld-random5.md)
were pulled through `next(bench)`. All setup commands exited zero, all canonical
verifier calls completed, and all worker stop records reported complete cleanup.
The untouched tasks scored zero. The three Calc documents and the 17-slide
Impress presentation appeared as expected. GIMP's first screenshot caught its
color-profile dialog before its contents finished drawing; a separate repeat
showed the complete prompt for `dog_with_background.png`. No task answers,
reference repository files, template settings or engine behavior were changed.

The initial five-task audit timer started after acquisition, so its
`ready_seconds` values are not acquisition measurements. The script now includes
the pull duration. A separate GIMP repeat measured 7.80 seconds for checkout and
setup from a preloaded sandbox, excluding initial pool startup.

Runtime remains `2026.09.14.2`, engine commit
`942b66253d03cf260d2ae8a1665dfabb8723b2d2`. The desktop's existing parity limits
are recorded in [OSWorld acceptance](osworld-acceptance.md).

Reproducible checks live in `tests/test_benchmarks.py` and
`tests/integration/test_benchmark_pull.py`. For live tests, set the existing
`SANDWEAVE_WEAVE_INTEGRATION` disposable-worker root and
`SANDWEAVE_WEAVE_SLOTS=4`. The OSWorld audit script accepts `--pull` alongside
its existing task selection options. Raw logs and screenshots are retained in
the ignored `runs/osworld-acceptance/20260914-pull`,
`runs/osworld-acceptance/20260914-pull-timing`, and
`runs/docs-acceptance/20260914-pull` directories; live test logs are in
`/scratch/pranjala/sw-osworld-20260914/pull-*-live.log`.

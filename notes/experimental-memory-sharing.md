# Experimental memory sharing

Sandweave 0.2.28 separates guest allocation limits from admission reservations:

```python
from sandweave import Memory, Sandbox

env = Sandbox(memory=Memory(guest="16GiB", reservation="4GiB", experimental=True))
```

The worker and Weave reserve 4 GiB plus the full runtime allowance. The existing
guest allocator still limits guest pages to 16 GiB. RAM is populated as the guest
uses it; no timer, per-command RPC, new global lock, or memory sampler is added.
Omitting `reservation` retains the previous full-budget admission behavior.

This is **overcommit**, not a protected RAM guarantee or a weighted memory
controller. A reservation does not prevent a borrower from consuming memory
that another guest later needs. Live private data cannot be reclaimed like CPU
time. Aggregate demand above available host memory can trigger the host's OOM
killer, affecting borrowers, other sandboxes, or other processes in the same
allocation. The explicit experimental opt-in acknowledges that behavior.
Disk-backed memory remains a separate option with its existing kernel RAM cap.

Reservations are validated in the public API and at admission. Runtime overhead,
service reservations and image-import allowances remain counted. Concurrent
worker creation retains the existing guard through publication; controller
generation and reservation semantics are unchanged. Older controllers/workers
are detected before using the feature, so mixed versions cannot silently use
different accounting policies. This change needs no new engine binary.

Pools and templates carry the same Memory value. Snapshots retain it; restore
can replace or remove the reservation because admission is not captured guest
state. Guest/runtime/disk limits must still match for a live-memory restore.
The CLI uses `--memory-reservation` with `--experimental-memory-sharing`.

## Live acceptance, 2026-09-25

Three integration cases passed on Babel using isolated disposable workers,
the published 2026.09.24.1 runtime, and the existing unprivileged allocation.
The physical host has more RAM than the test budgets: these checks establish
admission and borrowing, not safe behavior during aggregate host OOM.

- A worker configured for 32 GiB refused a second ordinary 16 GiB guest because
  runtime overhead also counts. With sharing enabled it started four 16 GiB
  guests concurrently, reserving 18 GiB total. One guest wrote every page of a
  5 GiB mapping and retained it while three peers completed 60 commands. Their
  median latency was 94 ms and maximum 273 ms on the worker's two eligible CPUs.
  After that mapping was freed, a second guest also allocated and verified 5 GiB.
- A Weave pool started four simultaneous 1 GiB guests across two workers with
  1 GiB admission budgets. Each guest reserved 128 MiB plus 256 MiB runtime.
  A 2 GiB allocation failed at the guest limit; all four sandboxes still ran
  commands, and the next pool checkout had pristine filesystem state.
- A live snapshot preserved a process holding 192 MiB. Restores succeeded
  with both the original 128 MiB reservation and full guest reservation,
  retaining the process's memory and stdin behavior.

All test sandboxes were terminated and the dedicated workers/controllers shut
down. The [receipt](memory-sharing-acceptance-20260925.json) contains measurements
without worker credentials. Reproduce with `SANDWEAVE_WEAVE_INTEGRATION` pointing
to a disposable directory and `SANDWEAVE_ASSETS` to a complete prepared runtime:
`pytest tests/integration/test_memory_sharing_live.py`.

The initial test attempt selected this historical lab checkout as its assets;
it lacked the prepared `passt` helper and stopped before guest startup. The
successful run used the previous release's complete prepared assets. Initial
unit-suite subprocesses also found an obsolete SDK in the test interpreter;
the affected cases passed with an absolute source import path. Neither issue
required a product-code workaround.

Host tests cover opt-in and size validation, raw-wire validation, default
compatibility, service/import accounting, the existing disk-memory cap,
template/CLI/info serialization, 128 concurrent admission attempts, snapshot
overrides, and old-controller/worker rejection with reservation release.
The resources documentation was rendered and checked in desktop and mobile
viewports, including copying the new example.

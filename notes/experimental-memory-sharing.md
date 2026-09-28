# Experimental memory sharing

Sandweave 0.2.28 separates guest allocation limits from admission reservations:

```python
from sandweave import Memory, Sandbox

env = Sandbox(memory=Memory(guest="16GiB", reservation="4GiB", experimental=True))
```

The worker and Weave reserve 4 GiB plus the full runtime allowance by default. The existing
guest allocator still limits guest pages to 16 GiB. RAM is populated as the guest
uses it; no timer, per-command RPC, new global lock, or memory sampler is added.
Omitting `reservation` retains full guest-budget admission behavior.

Sandweave 0.2.38 adds an independent `runtime_reservation`:

```python
memory = Memory(guest="16GiB", reservation="4GiB", runtime="4GiB",
                runtime_reservation="512MiB", experimental=True)
```

Admission counts 4.5 GiB; guest and runtime limits remain 16 GiB and 4 GiB.
Omitting `runtime_reservation` counts the full runtime allowance. It accepts
32 MiB through the runtime cap, requires the same experimental opt-in, and can
be used without a guest reservation. `--runtime-memory-reservation` exposes it
in the CLI. No gVisor, helper or guest-image changes are needed.

This is **overcommit**, not a protected RAM guarantee or a weighted memory
controller. A reservation does not prevent a borrower from consuming memory
that another guest later needs. Live private data cannot be reclaimed like CPU
time. Aggregate demand above available host memory can trigger the host's OOM
killer, affecting borrowers, other sandboxes, or other processes in the same
allocation. The explicit experimental opt-in acknowledges that behavior.
Disk-backed memory remains a separate option with its existing kernel RAM cap.

Reservations are validated in the public API and at admission. Guest/runtime
reservations, service reservations and image-import allowances remain counted. Concurrent
worker creation retains the existing guard through publication; controller
generation and reservation semantics are unchanged. Older controllers/workers
are detected before using the feature, so mixed versions cannot silently use
different accounting policies. This change needs no new engine binary.

Pools and templates carry the same Memory value. Snapshots retain it; restore
can replace or remove the reservation because admission is not captured guest
state. Guest/runtime/disk limits must still match for a live-memory restore.
The CLI uses `--memory-reservation` and/or `--runtime-memory-reservation` with
`--experimental-memory-sharing`. Runtime sharing advertises a separate
`runtime_memory_reservations` capability, so a 0.2.28–0.2.37 worker/controller
cannot silently charge the full runtime cap for a request using the new field.

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

## Runtime reservation acceptance, 2026-09-27

Sandweave 0.2.38 passed four new live cases and three guest-sharing regression
cases using runtime 2026.09.26.6, with no engine or image rebuild. The
[receipt](runtime-memory-reservation-acceptance-20260927.json) contains only
resource settings and timings.

- A worker with a 1 GiB admission budget rejected an ordinary 128 MiB guest
  with a 1 GiB runtime cap. It admitted four concurrent instances with that
  same cap and 32 MiB runtime reservations alongside a fully reserved control
  sandbox: 960 MiB total reservations. The fifth instance was rejected, and
  terminating one instance released enough reservation for a replacement.
  The control sandbox remained responsive during concurrent guest commands;
  median command latency was 39 ms and maximum 141 ms.
- A four-member Weave pool ran across two workers with 1 GiB budgets. Each
  member had 128 MiB guest memory, a 1 GiB runtime cap and a 128 MiB runtime
  reservation. Worker admission counted 256 MiB per member, and the next
  checkout retained clean filesystem state.
- Filesystem and live-memory snapshots each restored four times with inherited,
  reduced, explicit full and scalar full runtime reservations. The 512 MiB
  runtime guard stayed unchanged. The live process retained its 96 MiB mapping.
- A snapshot without the new field restored with a smaller runtime reservation
  and retained its process's 192 MiB mapping. Existing guest-only sharing and
  pool tests also passed.

The cap checks inspect the actual launch arguments. These tests verify
admission and state preservation, not safety during aggregate host OOM or a
specific reduction in real runtime RSS. All dedicated test workers and
controllers were shut down by their fixtures.

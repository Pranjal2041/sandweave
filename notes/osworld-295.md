# OSWorld unanimous 295 split

Sandweave 0.2.41 adds `Benchmark("osworld-unanimous-295", ...)` to the existing
OSWorld integration. The two implementation changes register the name and load
named subsets from their reference manifests. Pool admission, task leases,
desktop preparation, controls and evaluation use the existing paths.

The source remains cua-speed-run commit
`681f8dbc695ff3a7e3af2f532bec982725818211`, with canonical OSWorld commit
`315a7603173feadf1b8a85cbc006c93ffe1dc1a1`. Neither checkout nor template nor engine
was changed. The full 369-task list and Energy50 retain their previous meaning.
The 295 split follows its reference's unanimous inclusion review; it is not a
new claim of complete Sandweave runtime compatibility for all selected tasks.

## Validation

Artifacts are under `/scratch/pranjala/sandweave-osworld295-20260929` on
Babel u9-24. The task budgeted 20 GiB of additional local storage and checked
the 15% free-space floor before staging/launch and during monitoring. Runtime
acceptance used the existing Slurm allocation, without GPU, sudo or KVM.

- The real-source test downloaded the pinned sources through the public
  `Benchmark` constructor. All 295 unique IDs match the reference, in canonical
  order. Every task JSON checksum, instruction and all 17 setup patches match.
  Ordered task-ID digest:
  `c5964dbe0b19aa92c01992596c217d999bde292d1db34136a64a155b13b983a1`.
- The same check loaded all 369 upstream tasks and verified the existing 50-task
  subset, including its one setup patch and ordered-ID digest
  `8a577f2e475111d89c9840783fa431cd6f31655f071bdcc68fbc361a2ce2d22d`.
- The live desktop test used a separate worker home and local directory, reusing
  the previously qualified immutable OSWorld filesystem baseline. It called
  `Benchmark("osworld-unanimous-295", ...).next()` and received the first task,
  Chrome search-engine selection (`bb5e4c0d-f964-439c-97b6-bdb9747de3f4`), which
  is outside Energy50. Setup/readiness found the visible Chrome window.
- The actual 1920x1080 screenshot was opened and inspected: GNOME Ubuntu with
  its dock and Chrome's new-tab screen. The canonical verifier completed and
  returned zero for the untouched task. The task and pool were closed, and the
  disposable worker was shut down. The test passed in 146.53 seconds, including
  first-use worker preparation. This is one task smoke test, not a completed
  295-task agent evaluation or a fleet-throughput measurement.
- Unit regressions cover subset selection instead of full-list fallback,
  preserving order and patches, reusing cached task sources, replacing one
  corrupted cached task, and rejecting mismatched downloaded task bytes. The
  source host suite passed 800 tests with 11 optional skips; 217 integration/GPU
  tests were deselected. The real-source and live desktop tests ran separately.
- Documentation passed a strict build and browser checks: 23 pages, 118 Python
  examples, 1,931 internal links. The new example and copy button were checked,
  and the desktop/mobile screenshots were opened and inspected.

## Reproduction

With access to the pinned private repository and an explicitly selected test
home, validate the source lists without launching desktops:

```bash
SANDWEAVE_OSWORLD_SOURCES=1 python -m pytest -q -s tests/integration/test_osworld_sources_live.py
```

For a disposable worker with an existing prepared OSWorld filesystem cache:

```bash
SANDWEAVE_OSWORLD_DESKTOP_CACHE=osworld-ready python -m pytest -q -s tests/integration/test_osworld_295_live.py
```

The general acceptance script can also select this split:

```bash
python scripts/accept-osworld-benchmark.py --benchmark osworld-unanimous-295 \
    --sample 5 --pull --output /path/to/results
```

That last command is a reproduction option, not a claim that five new random
295-split tasks were executed for this change. Release validation installs the
committed wheel and records its selected checks under `runs/deploy/0.2.41-*`.

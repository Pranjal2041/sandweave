# Public CUA Speedrun source

Sandweave 0.2.44 switches the default OSWorld integration source to the official
repository linked by [cuaspeedrun.com](https://cuaspeedrun.com/):
[`cua-speedrun/cua-speedrun`](https://github.com/cua-speedrun/cua-speedrun), commit
`be17c72c5efbb145d06f86028336fdf2743a3d98`. It downloads the public source archive
without `gh` or authentication, checks SHA-256
`19769c938fb64b514080dd592b5c5d93c0543ad5ec4f92069aaa0c9df9bbd369`, and publishes
the extracted cache atomically. No reference source is vendored into the SDK.

The existing API is unchanged. `osworld-energy50-representative` reads the public
`osworld-50` manifest; `osworld-unanimous-295` reads `osworld-offline`. Both lists
exactly match the previous pinned source in membership, order, source JSON
checksums and setup patches. `osworld` retains the canonical 369-task list.

The desktop recipe (`osworld_modal_base.py`) and keyboard implementation are
byte-identical to the previous reference. The verifier differs only in its
docstring. Canonical OSWorld remains pinned at
`315a7603173feadf1b8a85cbc006c93ffe1dc1a1`; the official QCOW2 image pins and the
Sandweave runtime are unchanged. This switch does not establish broader VM parity.

The public setup hook resolves relative downloads under the desktop user's home,
sets ownership of newly created parent directories, and starts setup from that
home directory. Those upstream changes are used without patching the reference.
The public repository omits three legacy setup patches, all outside the 50- and
295-task subsets: `multi_apps` tasks `48c46dc7-fe04-4505-ade7-723cba1aa6f6`,
`b337d106-053f-4d37-8da0-7f9c4043a66b` and
`d1acdb87-bb67-4f30-84aa-990e56a09c92`. These full-list tasks use canonical setup.
An explicit unchanged private checkout at
`681f8dbc695ff3a7e3af2f532bec982725818211` is still accepted through `source=` for
reproducing old runs, with a distinct task cache and template identity.

## Validation

Evidence is under `/scratch/pranjala/sandweave-cua-public-20261019` on Babel u9-24.
The cold download and source test loaded all 369 tasks anonymously. Both subsets
match their public manifests, including every selected JSON checksum and all
one/17 setup patches respectively. Ordered ID digests remain:

- 50: `8a577f2e475111d89c9840783fa431cd6f31655f071bdcc68fbc361a2ce2d22d`
- 295: `c5964dbe0b19aa92c01992596c217d999bde292d1db34136a64a155b13b983a1`

The actual public setup hook also passed a local-fixture test of relative
download paths, working directory and ownership. Focused regressions cover
anonymous download without subprocesses, archive corruption, source pin and
modification checks, legacy subset paths, separate caches, stale-patch removal,
task pulling, preparation, readiness and input-command parity.

The disposable live test reuses the previously qualified immutable OSWorld
filesystem baseline because the desktop recipe and image are unchanged. It
uses the public source's setup and verifier. The first attempt failed before
launch because a reused evaluator-only interpreter lacked `aiohttp`; the retry
uses a separate SDK environment with read-only access to the existing evaluator
dependencies. No reference checkout or existing desktop was modified.

The retry completed Chrome task `af630914-714e-4a24-a7bb-f9af687d3b91` through
`Benchmark.next()`, keyboard/mouse actions and `task.evaluate()`. The canonical
score was 0 before actions and 100 afterward. Both actual 1920x1080 screenshots
were opened: the initial Chrome new-tab desktop and the final appearance screen
showing “Very large” font size. Setup/readiness logs and action screenshots are
retained with the results. This is one completed task, not a full-suite evaluation.

The focused source test run passed 51 regressions plus two real-source checks.
Documentation passed its strict build and browser checks (24 pages, 120 Python
examples, 2,014 internal links); the public-source section was opened and checked.
Storage admission budgeted 40 GiB and kept more than 15% of scratch capacity free.
Installed release validation is recorded under `runs/deploy/0.2.44-*`.

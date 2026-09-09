# SDK implementation and acceptance

The user approved implementation around two pillars: templates define setup,
startup and controls; sandboxes implement running instances and their lifecycle.
The README's public API remains the contract, including command strings.

## Work in progress

- Package the existing qualified engine mechanisms as installable assets; give
  each worker an isolated workspace without changing the lab's local-path file.
- Implement templates, command/process/files APIs, lifecycle and snapshots,
  preparation caching, template-owned controls, pools, async and CLI parity.
- Exercise local, SSH/Slurm placement and the explicit Apptainer runtime.
- Run existing host regressions and disposable CPU/GPU integration, then SDK
  acceptance for every supported feature. Preserve unsupported-feature failures.
- Build/install the wheel outside the checkout and inspect actual desktop/VR
  output, including both eye videos. Record current evidence separately from
  historical acceptance and keep all authored changes committed.

## Resources and preservation

The chat runs on `babel-p9-16`, which has unrelated user workloads. SDK acceptance
requested its own L40S allocation: Slurm job `10367253`, preempt/preempt_qos,
12 CPUs, 96 GiB, three hours. Existing user jobs/environments are not owned by
this task. `previous_transcript.txt` remains untouched and untracked.

## Acceptance results

The allocation is running on `babel-u5-28` with one L40S. Current acceptance:

- 9 package foundation tests passed: binary framing, resource validation,
  template inheritance/fingerprints and sync/async method binding.
- A real gVisor command smoke test passed, including `/dev/kvm` absence and
  owned-runtime cleanup (18.28 s including initial workspace preparation).
- 3 public-API integration tests passed in 7.50 s: command strings/pipes,
  literal argv, Unicode files, transfers, command failures, streaming stdin/stdout,
  execution versus wait timeouts, setup, borrowed handles, pause/resume and async.
- 12 existing host regression suites passed. `test-fast-io-unicode.py` is a live
  harness probe, not a host unit suite; its attempted import lacked autoharness.
  It belongs in later desktop acceptance with its prepared harness environment.

Further public-API acceptance on the same owned allocation:

- Four cache/checkpoint tests passed (153.31 s): independent filesystem clones,
  immutable references versus moved names, integrity verification, live process
  RAM and stdin restoration, safe stop, failed-save recovery, preparation reuse,
  content invalidation and captured/prepared provenance conflicts.
- Pool isolation/order/concurrency, async command cancellation, and CLI command,
  name, output and exit-code tests passed. Used guests are discarded; pool
  preparation/refill and checkout are separate work.
- Desktop input/pause/restore acceptance passed after fixing startup readiness.
  Xvnc availability and an announced window manager were insufficient: creation
  now also waits for the GNOME session and desktop paint. The test exits GNOME's
  initial overview before directing input to its application. Actual `typed.png`
  and `resumed.png` were opened and inspected in `runs/sdk-acceptance/desktop`.
  The exact Unicode text reaches the application; this base lacks Japanese
  glyph fonts, which appear as missing-glyph boxes. Filesystem restore boots
  a fresh desktop and retains saved files.
- Portable VR frame/recording code was extracted without changing its format;
  all 12 existing VR stream host tests passed. The SDK VR adapter and two game
  templates are implemented but live acceptance is still underway. The first
  GPU attempt timed out before GNOME readiness, before launching Monado/game.
  A disposable retained diagnostic guest is investigating the prepared base.

Remote/native adapters, resource admission/TTL/mounts, GPU/VR qualification,
remaining CLI parity, installed-wheel acceptance and the full feature matrix
remain in progress. No performance target or full-feature acceptance is claimed.

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

The allocation initially ran on `babel-u5-28` with one L40S. It was preempted
and requeued on `babel-q9-16`; durable SDK artifacts survived, live RAM did not.
Current acceptance:

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

- The Slurm target passed remote commands/files, borrowed handles, pause/resume
  and filesystem cache restore on the new node (26.30 s). A borrowed allocation
  remains running after its sandbox exits. Its worker runs in a persistent Slurm
  step, so ending a caller does not kill the worker. A separate CPU-only request
  was rejected by this site's GPU-required QoS and that owned request was cancelled.
- Native Apptainer and command regressions passed five live tests (21.20 s),
  including affinity, writable root setup, pause with open stdin, independent
  filesystem clones, deletion across restore, safe stop, integrity verification,
  and rejection of memory snapshots/filtered networking/CPU weights/GUI templates.
  The native adapter reports host networking, single-UID root mapping, CPU
  affinity and a sampled aggregate RSS guard; these differ from gVisor guarantees.
- The GPU desktop delay was traced to the prepared image's oneshot GNOME Keyring
  unit: its daemon caused a 90-second systemd stop timeout. The GNOME recipe now
  uses foreground D-Bus service readiness. Fresh prepared GPU desktop readiness
  passed in 40.97 s and the screenshot was inspected. Its initial overview still
  requires leaving overview before application keyboard input.

Further live acceptance on `babel-q9-16`:

- Real guest file descriptors support buffered text/binary streams, seek,
  truncate, append, exclusive creation and reading after rename. TTL survives
  client disconnect and expires paused guests. Memory admission counts both
  guest and runtime budgets, and live names are unique. These checks plus the
  four checkpoint regressions passed eight tests (57.73 s); subsequent stream
  and mount acceptance passed eight tests without warnings (28.57 s).
- `Mount(source, destination)` defaults to read-only worker-path access.
  External writable mounts reject capture unless explicitly marked
  `snapshot="rebind"`; rebind preserves shared external state independently
  of the sandbox timeline. Both runtimes passed real write protection,
  filesystem restore/rebind/removal, and failed-save survival checks.
- Open Saber and GunSpinning passed paired-eye observation, input ACK ordering,
  owned pixel buffers, retained Monado PID across pause/resume, paired recordings,
  rejection of graphics RAM capture, filesystem stop and cold restore. Both
  passed again with lossless Zstandard RPC transport (300.46 s for both games).
  Actual decoded left/right/SBS videos were inspected in
  `runs/sdk-acceptance/vr/opensaber-f6719313` and `gunspinning-d77ecd3a`.
  These particular clips show menus and tracked controllers, not shooting or
  level completion. The 20-FPS requested remote recordings delivered about
  12.6–13.6 paired captures/s; application submission FPS is a separate measure.
- GPU asset staging now makes deliberately guest-readable asset directories
  traversable under the worker's private umask. Private workspace ancestors
  remain private. Previously the desktop user could not load staged GPU tools.
  GunSpinning readiness now waits past its initial black frames.
- Three CUDA tests passed (71.50 s): gVisor/native compute and GPU readback,
  retained nonce/value across pause, filesystem persistence with fresh CUDA
  processes, and explicit experimental gVisor RAM/CUDA restore preserving
  nonce, PID, allocation address and contents. Capture and restore each require
  `experimental_gpu_live=True`; normal GPU graphics snapshots still fail.
- Cooperative MPS acceptance passed (31.13 s): distinct four/eight-SM clients,
  actual PTX kernel results, the 1 GiB per-client memory boundary, and a surviving
  peer after the first owner terminates. This is not hardware GPU isolation.
- Nested Docker acceptance passed (63.44 s): real Moodle and its nested MariaDB,
  RAM/open deleted file/offset/ownership/abstract socket/cross-network-namespace
  TCP checkpoint state, live web service after restore and pause/resume, and
  cold filesystem restore with the nested database still available.
- Guest CPU/memory views, a refused allocation above the guest page budget,
  continued guest liveness, host canary denial and internet/offline egress passed.
  The CPU-quota check initially looked up the wrong broker record key; after
  correcting the test it passed (10.49 s), with the measured rate recorded in
  `runs/sdk-acceptance/resources/quota.json`.
- Explicit `Slurm.acquire` succeeded with owned GPU job `10368361`; remote
  commands/lifecycle/cache restore passed, then its allocation context cancelled
  that job. Borrowed job `10367253` was preserved.
- 28 affected legacy environment/filesystem/VR host tests passed, plus two
  subtests. The extended CLI's agreed `desktop action --input` is implemented.

Gamepad/application gameplay acceptance, remaining CLI/async/extension details,
installed-wheel acceptance and the complete feature/performance report remain
in progress. No full-feature acceptance or startup performance target is claimed.

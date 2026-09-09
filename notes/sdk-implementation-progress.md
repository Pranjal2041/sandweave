# SDK implementation and acceptance

The user approved implementation around two pillars: templates define setup,
startup and controls; sandboxes implement running instances and their lifecycle.
The README's public API remains the contract, including command strings.

## Public PyPI release (2026-09-09)

[Sandweave 0.1.0](https://pypi.org/project/sandweave/) is published as a wheel and
source archive. Installation is now `uv pip install sandweave` or
`pip install sandweave`. The README uses a live PyPI version badge and absolute
documentation links. GitHub remains private; its documentation still requires
repository access.

The source archive now uses the wheel's declared engine inputs, excluding
unrelated lab scripts. Both archives include upstream notices for the bundled
patches. PyPI metadata and README validation passed, all 117 host tests passed
(one optional dependency case skipped), and rebuilding the source archive
produced identical wheel contents. A fresh environment installed from public
PyPI, imported the SDK, resolved all seven templates and ran the CLI. Its
installed package bytes and PyPI's reported hashes match the reviewed wheel.

The published description and live version badge were rendered and visually
inspected at desktop and phone widths. A CAPTCHA prevented inspecting PyPI's
full live page from this machine. Package downloads and API verification
succeeded. Runtime code is unchanged from the desktop-tested release source.
See [release files and verification](pypi-release-0.1.0.json); detailed artifacts
are under ignored `runs/pypi-release-spsK8F`.

## Desktop defaults and VNC credentials (2026-09-09)

New GNOME sandboxes default to 1920×1080. The template, source installation and
lab VNC configuration agree; custom template resolutions remain supported.
Resolution changes now follow Shell's completed startup. The first acceptance
attempt resized during startup and then timed out waiting for Shell's completion
message. The final run passed after reordering those steps.

`env.info["vnc"]["password"]` reads the installed credential through the guest
command API, including on existing workers. It checks the encrypted password
file before returning either a generated password or the older lab default.
Missing or mismatched credentials return `None`; reading does not reset them.
The README shows the field and no longer includes the redundant GitHub badge.

The final installed wheel passed all three desktop/information integration
cases from `/tmp` in 79.11 seconds. Checks completed VNC authentication using
the advertised generated and legacy passwords, read a 1920×1080 framebuffer,
compared CLI/Python summaries, rejected stale credential metadata, and exercised
keyboard input, pause/resume, filesystem restore and a 1280×800 override.
The 117 host cases passed; one optional dependency case was skipped.
All 54 package sources and 54 engine inputs matched the wheel and installation.

The actual first desktop, restored desktop and typed-input screenshots were
opened and inspected, as were the GitHub-rendered README at desktop and phone
widths. The existing missing Japanese font glyphs remain visible in the input
test; the application receives the exact Unicode text. Evidence is under ignored
`runs/desktop-defaults-Gy7uoN`. It also retains the initial startup timeout and
an invalid rerun that overlapped wheel reinstallation; the latter failed on
temporarily missing installed files. The final run began after installation
completed. All nine task sandboxes were stopped, no task worker remains, and
both original user desktops remained ready and running.

## Sandbox information (2026-09-09)

`env.info` and `sandweave info ID` collect lifecycle state, template/runtime,
worker hostname, configured CPU/memory budgets, selected GPU identity
and VNC connection details. VNC URLs are worker-local; users choose how to
connect from another machine. Coding guests' reserved VNC ports and stale ports after
pause/termination are not advertised as ready desktops.

The initial implementation included generated SSH commands and Slurm job IDs.
The public summary now omits both, at the user's request. The summary checks and
usage examples follow that revision; existing SSH and Slurm placement remain
available through targets. The revised summary passed all 117 host cases, with
one optional dependency case skipped. This revision changes summary output;
the runtime acceptance below records the original feature run.

The initial host suite passed 117 cases, with one optional dependency case skipped.
Coverage includes fresh state, private payload exclusion, older worker data,
SSH/Slurm aliases, and UUID-based GPU identification. Adding the integration
module initially exposed a duplicate test filename during whole-suite
collection; renaming it fixed collection, and the full host suite passed.

Five installed-wheel integration cases passed from `/tmp`: GNOME/VNC/CLI and
both command runtimes in 45.29 seconds, then both GPU runtimes in 43.13 seconds.
The GNOME summary matched guest CPU count and `/proc/meminfo`, its advertised
port returned an RFB banner, and pause/resume/termination updated the summary.
The dedicated preempt L40S job `10376165` on `babel-o5-28` verified that gVisor
and native Apptainer reported the exact model and UUID seen by guest
`nvidia-smi`, including the physical `/dev/nvidia5` selection. It completed with
exit code zero. Remote alias formatting had host tests; this change's live VNC
check ran on the worker without an SSH tunnel.

Artifacts are under ignored `runs/sandbox-info-wuourK`: `host.xml`, `wheel.xml`,
`gpu.xml`, the JSON summaries, `wheel-sources.json` and `cleanup.json`.
All nine changed package modules match the wheel and installed source bytes.
All eight disposable sandboxes (including three earlier source-checkout
checks) were terminated, their workers shut down, and both original user
desktops remained ready and running. CPU/memory values remain configuration,
not utilization measurements; [field semantics](sdk-usage.md#inspect-a-sandbox)
describe runtime differences and unavailable metadata on older workers.

## Process ownership and detached environments (2026-09-09)

New sandboxes now follow their creating Python process by default, including
crash cleanup. `detached=True` preserves them after that process exits. CLI
`create` remains persistent; borrowed handles and explicit context cleanup keep
their established behavior. Local process monitoring and remote heartbeat
expiry run in the worker, independently of Python exit hooks.

The final wheel passed 13 ownership cases, including SIGKILL, interrupted
startup, detached/paused guests, native Apptainer and GNOME. The host suite
passed 102 cases; 13 existing pool/CLI/lifetime cases passed in the focused
regression run. All 68 test environments were terminated and original user
desktops remained running. See [ownership implementation and evidence](process-ownership.md).

## GNOME startup profiling (2026-09-09)

The [startup investigation](gnome-startup-profiling.md) reproduced the user's
34-second desktop launch and traced 25 seconds to failed AccountsService
activation. Guest configuration now runs before systemd, and desktop readiness
includes GNOME's completed startup and a closed initial overview. The VNC
clipboard configuration window and expected first-use screen-lock notice are
hidden. Startup phase timings are exposed through `env.timings`.

The installed wheel started a clean desktop in 9.52 seconds of worker readiness,
9.82 seconds end to end. CPU desktop memory restore measured 1.64–1.95 seconds;
warm pool checkout including the first screenshot measured 12.98–15.86 ms.
Application input, pause/resume, cold filesystem restore, memory restore and
actual screenshots were checked. Original user desktops remained running.
See the linked investigation for sample scope, preparation costs and artifacts.

## Current status

Sandweave 0.1.0 implements the agreed Python/CLI contract, with the limits in
[usage](sdk-usage.md). The two-pillar structure and locked README examples are
preserved. The entries below retain the investigation history; later passing
runs supersede earlier failures and “in progress” observations.

The final feature run passed **62 tests in 761.40 seconds**: 46 real-runtime
integration cases plus 16 SDK host cases, with no skipped or failed tests. This includes
CUDA/MPS, desktop/VR, nested Docker, resource/network controls, snapshots,
mounts, lifetime, pools, async operations and guest terminals. This includes the final
concurrency and transport fixes. The separate host run passed 107 tests plus
16 subtests; after removing overlap, that is 153 unique automated tests, plus
the standalone application/placement/scale checks.

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

## Integration refinements and broader acceptance

The combined run exposed issues absent from individual tests. Each was fixed
before repeating the affected checks:

- Concurrent asset hard-link creation changes inode ctime without changing bytes.
  Fresh images now carry a pinned SHA-256 in the asset registry. Verification
  permits ctime-only churn only when the complete content hash still matches;
  unpinned frozen sources retain strict identity checks. The 7,819,030,528-byte
  base independently matched `e3d32dfc98c01c46e99dba43a0e3bbb28dbe4cc60d1c823ae27f44e4e5222b77`.
  Three failed artifacts created by this task were reverified successfully after
  adding that independently established pin; original reports/manifests remain
  in `runs/sdk-acceptance/snapshot-repair`. No user snapshots were edited.
- The guest HTTP service previously left its request-body timeout on idle
  keepalive connections. A 65-second idle regression now passes. Uncertain
  transport delivery still produces an explicit error rather than replaying work.
- Worker identity includes CPU/GPU/memory eligibility. One process owns each
  workspace. An acknowledged shutdown publishes its transition before removing
  its endpoint, preventing the next client from mistaking a closing worker for
  a failed live authority.
- Fast command output can arrive between an empty read and exit observation.
  Streams and CLI output now perform their final drain after confirmed exit.
- Command output spooling is bounded; terminal groups, streaming stdin, startup
  deadlines, retained diagnostics, async pool iteration/cancellation and CLI
  named pools have real-guest tests. Installed provider identity participates in
  preparation fingerprints and client/worker compatibility checks.

A combined run passed **53 tests in 812.26 seconds** before the final PTY/async
file-stream additions. Terminal tests subsequently passed for both runtimes,
including terminal resize, a controlling shell, and an open terminal restored
from RAM. All 15 terminal/command/pool tests passed in 104.35 seconds. The final
expanded combined run is recorded separately below when complete.

The pinned CUA harness passed **100/100 entries and 500/500 repetitions**, with
zero flaky, failed, unsupported or error cases, through the SDK desktop API.
Of its screenshots, 480 visual verdicts matched the probe's final state; 20
headless raw-tap checks have no visual oracle. A decoded probe screenshot was
opened and inspected. Evidence: `runs/sdk-acceptance/cua-full`.

GunSpinning's gamepad mode reached the training range and fired through the
SDL input provider; its actual screenshots were inspected in
`runs/sdk-acceptance/gameplay/gamepad-1`. This is flat mode. The separate motion
controller run reached the VR training range, fired, reloaded and moved head
and controller poses. Its 698-frame left/right/SBS videos have identical,
strictly increasing timestamps and were decoded and inspected. Evidence:
`runs/sdk-acceptance/gameplay/motion-2`.

The motion recording delivered **20.97 stereo captures/s**, with no recorder
queue drops, at 960x1080 per eye. Ring frames skipped between captures are
reported separately. Aligning Monado metrics to the recording's guest clock
measured **80.71 application submissions/s over the recording**, and **83.39/s
in its final ten seconds**. The earlier 85.18/s figure came from a later window
while videos were being exported. `aligned-metrics.json` keeps these scopes
explicit; none of these measurements establishes physical headset scanout.
Raw RPC transfer measured 28.06 ms median versus 37.57 ms with Zstandard in
that particular run; compression reduced each pair from 8.29 MB to 2.22 MB.

An installed wheel ran from `/tmp`, loaded its bundled engine, and passed
commands, filesystem restore, pools, generic SSH attachment and a separately
installed point-mass provider with independent saved-state restoration. The
point-mass example is a toy extension test, not a RoboCasa claim. Initial
performance evidence is `runs/sdk-acceptance/installed.json`; it was collected
while other acceptance work used the same allocation.

The CLI acquired GPU job **10368765** on `babel-n5-28`. A verified snapshot was
imported from `babel-q9-16`, and a pool ran six independent ordered tasks across
both worker workspaces. The CLI cancelled its owned job and preserved borrowed
job `10367253`. Evidence: `runs/sdk-acceptance/multiworker.json`.

Local GPU discovery, with Slurm identity removed inside the existing allocation,
respected retained device visibility and passed CUDA initialization. This tests
the non-Slurm discovery path on qualified hardware, not a fresh workstation.
Weighted CPU sharing measured 2.84:1 for 3:1 weights and about 6.48 CPU equivalents
for the surviving peer after idle capacity became available. The controller
failure drill passed after distinguishing its quota pause from an SDK pause.
Only the dedicated test guest's broker was killed, after checking it had no
other registrations.

The EROFS fixture probe now accepts a private SDK workspace instead of depending
on the original lab's node-local path. Actual native/guest data, binary/empty/
shared xattrs, named ACL grants and mask denials, and read-only enforcement all
passed. Earlier attempts failed on missing legacy path assumptions before
reaching the engine. Evidence: `runs/sdk-acceptance/erofs-final2.log`.

The release host suite passed **105 tests plus 16 subtests**. This count includes
the 14 SDK host tests; do not add those again when combining counts.

## Final package and scale qualification

The final installed wheel passed from `/tmp`, outside the checkout. Its 180
packaged SDK/template/engine source files match the canonical source bytes.
Commands, saved filesystem state, independent pools, real terminals, async file
streams, a separately installed control provider and generic SSH attachment
passed. Evidence: `runs/sdk-acceptance/installed-final.json` and
`wheel-sources.json`. The wheel remains dependent on explicitly prepared assets.

A 32-thread shared-filesystem lock reproduction exposed stalled concurrent
`flock` waiters. Stack traces were preserved and only that test's clients and
sandboxes were retired. Lock acquisition now serializes same-process threads
before entering the filesystem lock and explicitly unlocks on every exit.
The isolated 3,200-acquisition probe passed in 1.42 s. The full pool drill then
held **32 simultaneous independent leases**, ran **64 clean ordered tasks**,
and completed in **20.85 s**, including 10.11 s baseline/eight-warm preparation,
9.87 s mapping and cleanup. Evidence: `scale-first-failure.json`,
`scale-client-stacks.txt` and `scale-32.json` in `runs/sdk-acceptance`.

Small HTTP replies now buffer their headers and body together. Diagnostic runs
isolated intermittent approximately 40 ms transport delays; buffering reduced
fragmented small replies, but no claim is made that all remote latency spikes
are eliminated. The final installed-package benchmark ran after the other
acceptance processes finished on the dedicated 12-CPU L40S allocation:

| Operation | Samples | Median | p95 |
| --- | ---: | ---: | ---: |
| Cold guest plus first Python command, existing worker | 19 | 812.82 ms | 897.98 ms |
| Existing guest, shell `true` | 100 | 15.43 ms | 27.11 ms |
| Existing guest, literal argv `true` | 100 | 15.29 ms | 21.44 ms |
| Filesystem restore plus first command | 20 | 1067.94 ms | 1113.38 ms |
| Ready pool checkout | 30 | 0.059 ms | 0.068 ms |
| Ready checkout plus first command | 30 | 24.83 ms | 47.96 ms |
| Wait for warm reserve refill | 30 | 732.90 ms | 800.09 ms |

The first worker preparation plus first guest/command took 7.21 s. Scheduler
queue time is excluded because the allocation already existed. These are
measurements on this allocation, not cross-machine guarantees. Cold creation
has not met a few-millisecond target. A warm handle alone is not completed work.

Visual review of the previous restore screenshots found first-painted splash
screens. The final VR test therefore adds sustained input and paired recordings
after cold restoration. Initial game recordings have already been decoded in
full, their left/right/SBS timestamps matched, and both eye images inspected.
The post-restore recordings are reviewed separately in the final result below.

The final host regressions passed **107 tests plus 16 subtests in 1.17 s**,
including 16 SDK host tests. Historical application workflows such as Earth,
Resolve and Alyx were not all repeated in this SDK task. The CUA catalog,
GunSpinning gameplay and the current runtime feature tests establish their
stated scopes; they do not upgrade historical or experimental features to
universal support. The optional directory-artifact API in the proposal remains
future work; whole-sandbox filesystem capture is implemented and tested.

## Completed release acceptance

The final combined run passed **62/62 tests in 761.40 seconds**, with no errors,
failures or skips. Evidence: `runs/sdk-acceptance/release-qualified.xml`.
All 27 snapshot verification reports in that run's private workspace passed,
and no sandbox remained in a creating, preparing, ready or paused state. Stopped
and negative-test records are retained for evidence.

Open Saber (`opensaber-69571713`) and offline GunSpinning
(`gunspinning-4ad0781b`) passed the extended cold-restore check. Both original
and restored sets contain real left-eye, right-eye and synchronized stereo
videos. All twelve videos decoded completely and have identical, strictly
increasing timestamps within each set. Decoded frames from both eyes show each
game's menu and tracked controllers after restoration. The separate GunSpinning
training-range recordings above establish shooting gameplay.

The installed wheel and a wheel rebuilt from the source archive contain the
same 180 package source files. Authored implementation, tests, examples and
documentation are committed; generated packages, runtime state and recordings
are deliberately ignored. The [machine-readable acceptance report](sdk-acceptance.json)
pins report/video hashes, package identity, source identities, measured timings
and the limits of the claims.

Final cleanup checked 27 task-owned worker endpoints and cancelled only the
owned `sandweave-e2e` allocation `10367253`; Slurm confirms `CANCELLED`.
Saved artifacts remain on durable storage. Existing user jobs/desktops and
`previous_transcript.txt` were preserved.

## Guided setup and doctor (2026-09-09)

`setup` now prepares a local worker through workload selection, runtime-file
selection, optional dependency installation, worker staging and a disposable
coding check. `doctor` offers repairs in an arrow-key terminal menu and reruns
checks after a repair. `doctor --check` and `--json` are noninteractive and do
not install or change configuration. The older `setup ID SCRIPT` and
`configure --assets` commands remain available. The README now uses setup/doctor
and commented configuration examples; its original 12 agreed code blocks are
unchanged.

The terminal flow follows the short installation and task examples in
[Flutter](https://docs.flutter.dev/platform-integration/linux/setup),
[Prime RL](https://github.com/PrimeIntellect-ai/prime-rl#setup),
[Prime Agent](https://github.com/PrimeIntellect-ai/prime-agent/blob/main/packages/coding-agent/docs/quickstart.md)
and [Prime environments](https://docs.primeintellect.ai/tutorials-environments/getting-started).
The interactive repair behavior is Sandweave's addition.

Validation on the existing CPU allocation:

- `python -m pytest tests -q`: 34 passed; the 46 opt-in integration cases were
  skipped. New tests cover corruption, path escape, asset selection, decline
  and cancellation, preserved configuration, noninteractive checks, and actual
  encoding/decoding of distinct paired-eye fixtures through managed FFmpeg.
- `python scripts/test-vr-stream.py`: 12 passed.
- Live `setup --yes --template coding` staged files and ran `print(2 + 2)` in a
  disposable sandbox. Doctor's actual terminal menu was exercised for cancelling
  runtime selection, accepting it, declining a package installation, and
  accepting FFmpeg installation. Existing target configuration survived.
- `scripts/sdk-onboarding-acceptance.py` passed with two concurrent workers using
  different prepared asset roots. The original guest remained usable and its
  file survived; the new guest was independent. A borrowed Slurm worker also
  ran through the managed Apptainer executable. Test guests were terminated and
  their workers shut down through `_shutdown_if_idle`; the allocation was retained.
- A VR-profile doctor check on the CPU allocation reported its missing GPU,
  while the host runtime, game-file checks and FFmpeg encoder probe passed.
  This was a prerequisites check, not a new VR gameplay acceptance run.
- Python/TOML/bash snippets, 24 documented CLI invocations and README local links
  passed syntax/parser/link checks. Language and implementation audits were
  completed by separate agents.
- The rebuilt wheel matches all 181 package/engine source files, declares the
  terminal-menu dependency, and its installed doctor command passed from outside
  the checkout using saved runtime configuration.

Reproduce the worker checks with an existing allocation and a fresh output path:

```bash
PYTHONPATH=src python scripts/sdk-onboarding-acceptance.py \
  --assets "$PWD" --output "$PWD/runs/my-onboarding-check" --slurm-job JOB_ID
```

The live outputs are under ignored `runs/sdk-onboarding-20260909`,
`runs/sdk-onboarding-tui-20260909` and
`runs/sdk-onboarding-switch-final-20260909`. The source-checkout installation
still depends on prepared images: no public runtime download bundle is
available. The pinned upstream Apptainer installer is supported but was not run
in this acceptance because Apptainer was already installed. It requires curl,
rpm2cpio and cpio; a missing prerequisite remains a reported failure. Existing
Apptainer detection and managed-tool execution were exercised instead.

## Setup storage and interrupted staging repair (2026-09-09)

The user's first setup selected runtime files on the data filesystem but staged
copies under `~/.local/share/sandweave`, on a separate home filesystem. Its
staged EROFS image contained 2,565,341,184 bytes instead of 7,819,030,528 bytes.
The old staging helper accepted any existing file, and a subsequent setup
published the incomplete image as prepared. gVisor then returned `bad address`
while resolving the guest's Python executable. The surviving logs do not show
what interrupted the original copy; home had about 5.5 GiB available when checked.

First setup now saves data under the selected runtime directory's `.sandweave`
subdirectory, prints the location, and writes a small location setting in home.
An explicit `SANDWEAVE_HOME` or an already saved location takes precedence.
Workers pin their own data directory. Existing files are preserved when the
default changes. Failed data-location selection stops setup before staging.
Copies now use temporary files, verify their length, and publish atomically;
prepared workspaces recheck staged lengths. The initial question now reads
"What do you want to start with? (You can add more later)".

Validation: 45 host tests passed; 46 opt-in integration cases were skipped.
The new tests exercise disk-full errors, interruption, silent short copies,
retry, truncated prepared images, location persistence, and failed location
selection. The actual `.venv/bin/sandweave setup` terminal flow completed with
data at `/data/user_data/pranjala/general-vm/.sandweave`. Its staged base image
has the full length and shares the source inode. A CLI command run from `/tmp`
returned `4`; filesystem cache/restore, concurrent workers using different
runtime sources, and a borrowed Slurm worker also passed. A real 8 MiB copy
between `/tmp` and the data filesystem repaired a partial destination and
matched the source's SHA256. The wheel was rebuilt.

Evidence is retained under ignored `runs/sdk-staging-repair-20260909` and
`runs/sdk-storage-acceptance-20260909`. Only the idle worker owning the two
failed setup attempts was shut down; its original home files and logs remain.
The existing allocation and other user environments were preserved.

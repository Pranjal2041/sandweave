# Guest clocks

Sandweave 0.2.45 and runtime 2026.10.24.1 give each gVisor sandbox its own wall
clock. The public guide is [docs/clock.md](../docs/clock.md); this note records
the implementation, acceptance and limits.

## Requirements

A client asked for a per-sandbox wall-clock offset with these properties:

- Guest `CLOCK_REALTIME`, through system calls and the vDSO, reads host time
  plus the offset. File times follow it.
- The monotonic clock stays real.
- The offset is chosen when a sandbox starts or restores, never baked into a
  checkpoint. Machines given the same offset agree on the time.

The agreed API uses flat keywords: `clock_offset` (seconds or `timedelta`),
`clock_start` (an aware `datetime` or ISO 8601 string) and `clock_pause`
(`"freeze"` or `"continue"`). A clock setting freezes guest time during
explicit pauses unless `clock_pause="continue"`; without one, pauses keep the
previous behavior. Freezing stops both wall and monotonic time. Host-mounted
volumes were explicitly out of scope.

## Engine

The change is `sources/gvisor` commit `7c3d390`, on `feat/guest-clock` above
`6bf5da1`, and is included in [the cumulative patch](gvisor-no-kvm-prototype.patch).

- `runsc` gains `--clock-offset` (nanoseconds), `--clock-start` (nanoseconds since
  the Unix epoch) and `--clock-pause=continue|freeze`. The first two exclude
  each other and are bounded to 2^62 ns.
- The Timekeeper already kept an unsaved monotonic offset recomputed by every
  `SetClocks`. A realtime offset now follows the same pattern: it is derived
  from the boot or restore configuration, applied in `GetTime` and added to the
  realtime base published in the vDSO parameter page. The page layout and the
  vDSO code are unchanged, so processes restored from a checkpoint read the new
  offset through their existing vDSO mapping.
- Saves record host realtime. Restore still advances monotonic time by the
  host time elapsed since the save, independently of the saved or restored
  offsets.
- `BootTime` is guest realtime less guest monotonic time, as on Linux, so
  `/proc/stat` and `/proc/uptime` stay consistent across offsets, freezes and
  restores. A process from a snapshot restored with a larger offset reports a
  `/proc/[pid]/stat` start time no earlier than boot.
- `runsc pause` with `--clock-pause=freeze` records frozen realtime and
  monotonic values, marks the vDSO parameters not ready, and pauses timers and
  Timekeeper updates with the same `pauseTimeLocked` used by saves. `runsc
  resume` sets both offsets so time continues from the frozen values, then
  resumes timers. Nested freezes are counted. A checkpoint taken while frozen
  leaves time stopped until resume.
- CPU broker throttling uses `RequestCPUPause`, not the controller's pause, so it
  never freezes time. A live checkpoint's internal kernel pause does not freeze
  time either.

Unit tests cover offsets, start dates, save/restore with host realtime,
restores without an offset, freeze/thaw, the monotonic lower bound and
AfterFunc timers during a freeze. `//pkg/sentry/kernel`, `//pkg/sentry/fsimpl/proc`,
`//pkg/sentry/ktime` and `//runsc/config` pass. The boot suite passes when run with
a world-writable temporary directory, as recorded in
[resource-snapshot-implementation.md](resource-snapshot-implementation.md). Remaining
nogo findings are in existing `pkg/tcpip/stack` code; none are in changed files.

## SDK

`definition()` builds a `clock` entry for each launch. It never copies one from
a cache or snapshot spec. The gVisor driver passes `--clock-offset-ns`,
`--clock-start-ns` and `--clock-pause` to `run-gvisor.py`, which forwards them
to `runsc` and leaves them out of the saved launch settings. The driver
requires an engine whose `runsc flags` include the clock flags; a memory
snapshot keeps its captured engine and is rejected if that engine predates
them. Workers and controllers advertise `guest_clock`. Clients, managed pools
and controllers reject clock settings on older workers instead of sending a
setting that would be ignored. Prepared engines without the `guest_clock`
capability are upgraded for new launches. `env.info["clock"]` reports the
configured setting.

## Acceptance

Measurements were taken on Babel `babel-u9-24`, Linux 5.14, in disposable
storage. The candidate runtime came from `scripts/build-runtime-release.py`
(runtime build `c49104ca…`). After packaging, `scripts/accept-runtime-release.py`
installed it from a local HTTP manifest and passed all checks: first CLI setup
took 76.0 s, a sandbox was ready in 1.43 s, and no compiler was downloaded or run.
Detailed results are in [guest-clock-acceptance.json](guest-clock-acceptance.json).
After publication, a fresh wheel with the new pin passed the same qualification
through automatic first use, with anonymous GitHub downloads: 82.3 s to the
first command and a 1.22 s ready sandbox.

| Check | Result |
| --- | --- |
| Offset −400 days + 0.25 s | vDSO `clock_gettime`, `time.time()`, `gettimeofday`, `time()`, syscall realtime and coarse realtime, `/tmp` and home-directory file times all read host + offset to within the 0.07 s command latency. |
| vDSO path | The first 20k-call window ran at syscall speed (34k calls/s) while the runtime recalibrated after idle; the best window ran at 10.2M calls/s, against 38k/s for direct syscalls. Readings taken afterwards still carried the offset. |
| Start date | Guest time started at the requested date and advanced with host time. |
| Frozen pause (6 s) | Guest realtime and monotonic time advanced 0.54 s across 6.69 s of host time. A 3 s sleep spanning the pause measured 3.001 s of guest time. A peer sandbox's 130 concurrent probes kept host time; median 68 ms, maximum 123 ms. |
| `clock_pause="continue"` and default | Guest time and a sleeping process both observed the pause. |
| Memory checkpoint | A ticker started before capture read +5 days after restore with `clock_offset`, and host time after restore without one. Monotonic time continued from the snapshot; saved file times were unchanged. |
| Checkpoint while frozen | Time stayed frozen until resume; a restored copy took a new −7 day offset. |
| Filesystem restore | A new start date applied; saved file times were unchanged. |
| Pool | Three members shared one offset. |
| CPU quota 0.25 with a busy peer process | The timed loop received 6% of a CPU, yet guest time kept host rate: 3.01 s of guest time over 3.56 s of host time. |
| 12 concurrent sandboxes | Members paused 0, 1 or 3 s twice. Each clock lagged by its own paused time plus about 0.25 s per pause/resume call (−2.5 s and −6.5 s); running members stayed within 0.03 s. |
| GNOME desktop | `clock_start=2025-03-01T09:00Z` showed "Mar 1 09:00" in the top bar. A 31.2 s pause advanced guest time by 1.07 s. A memory checkpoint restored with +100 days 5 h 30 min showed "Feb 2 01:04" (capture at 19:34:06 UTC), and the overview opened on input. |
| Weave | Forwarded and direct connections, a frozen pause through the controller, and a managed pool passed, with both directly reachable and outbound relay workers. |

The CPU throttling and concurrent-pause cases also check that freezing does not
affect unrelated sandboxes or broker scheduling. Screenshots were inspected; they
are retained locally and are not committed.

## Limits

- Host-mounted volumes keep host times. The gofer client does not write cached
  guest timestamps back on eviction, and shared mounts read host metadata.
- `CLOCK_MONOTONIC_RAW`, when nvproxy profiling exposes it, follows the host
  and is not frozen.
- Guest TLS and certificate checks use guest time.
- Workers' clocks bound agreement between sandboxes on different machines.
- A pause before freezing and the resume call itself take guest time: each
  pause/resume cycle adds roughly 0.1–0.3 s to the observed elapsed time.
- Memory snapshots captured on earlier engines cannot take clock options.

## Exact advance (2026-10-26)

`runsc resume --for=D` runs a sandbox paused with `--clock-pause=freeze` for exactly
D of guest monotonic time and leaves it paused (`containerManager.Advance`,
`Kernel.AdvanceClocks`). The kernel thaws time and tasks, sleeps D less 2 ms, freezes
time at the deadline (`Timekeeper.freezeAt`: never below a value a reader observed),
then pauses the tasks, so time stops at the deadline before any task can read past
it. A first version that paused the tasks first overran by 3.4 ms. The prepared
runtime advertises `clock_advance` 2. Live results are in
[games-action-space.md](games-action-space.md#game-time-on-desktop-games).

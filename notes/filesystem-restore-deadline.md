# Filesystem restore deadline — 2026-09-28

`Sandbox(..., startup_timeout=...)` already allowed a custom creation budget,
but `finish_boot()` separately imposed 300 seconds for boot staging and 600
seconds for each persistent mount. A restore could therefore fail despite
having time left in the caller's budget. `SANDWEAVE_SNAPSHOT_STALL_TIMEOUT`
only governs filesystem exports and never configured these restore limits.

In 0.2.39 the environment manager passes its monotonic deadline into the
detached launcher. Boot staging, each unpack and boot release consume the
same remaining budget. The worker's existing deadline continues through guest
agent and service readiness. The default is still 300 seconds; callers can use
`Sandbox(snapshot=saved, startup_timeout=1800)` or the same option on a pool.
The SDK CLI already exposes `--startup-timeout`. The standalone runtime launcher
now accepts that option too. No new SDK option or worker environment variable
is required, and the deadline is not stored as a snapshot resource setting.

Unpack control processes are killed and reaped on deadline expiry, launcher
shutdown or guest exit. The launcher joins its restore thread during cleanup.
The manager reports recorded restore errors instead of masking them with a
generic launcher-exited message. It reads the immutable manifest once per
launch, rather than again on each readiness poll.

The saved files remain available for another attempt. Existing images, snapshot
formats, engine binaries and the save-only stall timeout are unchanged. The
worker must run the new release; this fix requires no runtime archive rebuild.

## Validation

Unit coverage uses a virtual clock for a 350-second staging period followed by
two 650-second mount imports: an 1800-second budget succeeds, while a 1000-second
budget expires before starting the second mount. Other cases cover clipped
readiness probes, subprocess failure, deadline expiry, cancellation, guest exit,
reaping, error propagation, and preserving the deadline across launcher startup.

Live acceptance uses the normal gVisor engine and public SDK, with delays
injected only in a disposable worker's control wrapper. It verifies timeouts
during staging and unpacking, cleanup of both failed guests, and integrity of
the saved snapshot. Two concurrent successful restores preserve root, Docker
and containerd files. A separate sandbox continues serving commands throughout.
The final source run passed all three live cases. With one second of staging
delay and two seconds per mount, concurrent restores took 7.299 seconds; 59
peer commands had a median of 21.15 ms and a maximum of 51.58 ms. These are
measurements of this test, not latency guarantees. Long-duration boundary cases
use the virtual clock; the live test does not wait ten minutes per mount.

Evidence is under `/scratch/pranjala/sandweave-restore-timeout-20260928` on
`babel-u9-24`, in existing allocation `10533311`. The engine remains at
`6e21c7bb22ffa2ab8c51da4b4ce803aa26d7ba63`, runtime release `2026.09.26.6`.
Installed-package regression checks and artifact hashes are recorded by the
normal release receipt under `runs/deploy/0.2.39-<commit>/`.

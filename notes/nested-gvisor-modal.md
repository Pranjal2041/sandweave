# Nested gVisor on Modal GPU workers — investigation

This is an experimental compatibility branch, not a qualified runtime release.
Modal GPU workers expose a gVisor Linux ABI, currently reporting
`4.19.0-gvisor`; that version is not the underlying Linux kernel.

## SDK and Open Saber acceptance (2026-10-25)

The syscall-worker completion fix is committed as
`cc41508b33c307dc869ff0e2afc0e62551f291d4`. Full SDK coding startup now completes.
The worker launcher also handles hosts that omit `Umask` from `/proc/self/status`,
without temporarily changing the worker's mask during concurrent launches.
Its 14 tests pass on both the native CPU VM and the outer Modal gVisor host;
the release-selection tests pass on Linux (23 tests).

Two complete `Sandbox(template="vr/opensaber")` runs on the L40S produced
separate left/right videos and a synchronized stereo video. Run 13's decoded
left, right, and stereo videos were inspected at 60 seconds: they show gameplay,
both moving sabers, directional blocks, score 250, and the expected eye disparity.

| Run | Engine candidate | Capture duration | Saved stereo pairs | Capture FPS | Application FPS |
| --- | --- | --- | --- | --- | --- |
| `nested-opensaber13` | 2026.10.25.94 | 119.989 s | 3,564 | 29.694 | 40.564 |
| `nested-opensaber14` | 2026.10.25.95 | 120.001 s | 3,577 | 29.800 | 41.529 |

Both recordings requested 30 FPS and dropped zero frames in the recorder.
The application numbers come from Monado OpenXR submissions **during the
recorded window**, including level-loading stalls. They are not video FPS.
Median application frame intervals were 22.30 ms; the compositor targeted
90 Hz and reused application frames approximately 52–54% of the time.
Median compositor GPU work was 0.48 ms. These runs do not establish a
same-machine speed ratio against a single gVisor layer.

Candidate `.95` adds a `nested_gvisor` capability; its executable hashes are
identical to `.94`. The SDK now requires that capability on a gVisor host.
This matters for prepared images: the first VR trials retained their older
bundled `.93` engine despite a newer release pin. Run 14 exercised the normal
automatic upgrade from that prepared image. The acceptance script checks the
actual launch descriptor before recording.

**Remaining blocker:** startup is intermittent. A retained GNOME attempt
failed with `EBADF` from guest Python's `_posixsubprocess.fork_exec`, and a
concurrent guest-command investigation also observed a malformed control
stream. A separate 1,000-command concurrent run completed while Open Saber
continued running. Those results do not qualify this candidate for a stable
release. The failed guests, trace patches, and logs are retained for diagnosis.

Local evidence:

- `runs/nested-modal-20261025/vr-evidence.tar` contains both runs' videos,
  frame indexes, input acknowledgements, protobuf metrics, code, and reports.
  SHA-256: `4d0df9950700044ce9aa09c2d9aea6ce6888d8128780be101c0113d02eeca37d`.
  Lossless frame caches remain on the worker.
- Run 13's original summary used a post-export metrics window. Its separate
  `capture-window-summary.json` corrects that using the retained protobuf
  records and frame timestamps. Run 14 computes that window directly.
- `checkpoints/nested-modal-20261025/gvisor.bundle` contains verified complete
  engine history through `cc41508b`.
- Candidate `.95` archive SHA-256:
  `713d5a971f0e283d10b70b6774a4ce832706994b1d168fe680ffef9a7292787f`.

`notes/nested-sdk-startup-trace.patch` is diagnostic instrumentation for a
separate SDK copy; it is not included in the candidate wheel or engine.
The scripts `nested-gnome-check.py` and `nested-guest-command-check.py` retain
startup and concurrent-command results without changing readiness criteria.

## Initial engine acceptance (2026-10-24)

- The patched systrap engine runs `/bin/sh` and `uname -r` under the outer
  gVisor on a Modal L40S worker. Attached execution returns zero, with
  `NESTED_GUEST_OK` in its output. This is engine execution, not yet an SDK
  startup acceptance.
- With the same engine, `nvidia-smi -L` inside the nested guest reports the
  allocated L40S and exits zero. The physical device minor is 2 while its
  visible NVML ordinal is 0. The driver is 610.57.04.
- The CUDA kernel probe passes both directly on the worker and inside nested
  systrap: allocation, host-to-device copy, PTX compilation, kernel launch,
  synchronization and device-to-host copy return the expected value 42.
- Attached shell execution also succeeds with disk-backed root storage. The
  earlier proc-root permission failure is no longer reproduced by that probe.
- The standard `build-runtime-release.py` completes a clean coding-image
  build on a Modal CPU VM using the cumulative engine patch.
- The four SDK host-compatibility/release/onboarding/network test files pass
  on Linux: 99 passed, one skipped. The GPU inventory script passes nine
  tests. Those suites are not end-to-end GPU or VR acceptance.
- The host signal reproducer passes on the CPU VM. On the L40S, GS access
  returns EINVAL. With GS omitted, copying the CPUID-reported FP length over
  a guarded signal stack terminates with SIGSEGV; using the signal-frame
  length passes. CPUID reports 11008 bytes, the actual signal frame 2816.

## Engine changes and remaining qualification

The earlier startup fixes cover aligned address-space probing, gofer mount
propagation, and setting attached-process parent-death signals after entering
the new PID namespace. Additional systrap fixes are necessary:

1. The outer runtime does not implement ARCH_GET_GS. Probe support, omit GS
   preservation on hosts that cannot provide it, and reject guest GS requests
   there. Native Linux keeps GS support. Applications requiring GS remain
   outside this host's capabilities.
2. The outer runtime exposes an XSAVE size including hidden CPU state. Bound
   the systrap layout by advertised component offsets, enforce the context
   buffer limit, and copy only the supplied signal-frame bytes.
3. The outer runtime supplies zero REG_ERR and REG_TRAPNO values. Classify
   page faults with the ptrace platform's read/execute-then-write method.
   Mapping invalidations advance a generation to discard stale classifications.
   Guest VMA permissions still decide whether a fault is allowed.

The GPU probe uses **exclusive file caching for its private, disposable
mounts**. Without that explicit setting, mapping a binary on a shared gofer
mount fails with ENODEV because no host mmap FD is donated. Do not apply that
setting to arbitrary SDK user mounts. A share=container mount hint was also
tested and failed while creating a whiteout on the read-only GPU mount.
The full SDK needs a correct policy for its immutable driver resources.

Modal does not expose `/proc/driver/nvidia` in this worker. The branch reads
driver/device identity using the structured `nvidia-smi -q -x` report, retains
visibility and device-permission checks, and passes the exact driver ABI to
runsc. It does not assume that NVML ordinal equals device minor.

At this initial stage, Vulkan rendering, full SDK startup, snapshots,
concurrency, A10 acceptance, Open Saber, and stereo recordings were separate
gates. The initial probes did not establish a frame-rate or overhead result;
the subsequent SDK and VR results are reported above.

## Reproduction and evidence

- `scripts/modal-gpu-worker.py` creates workers and transfers files with
  verified checksums. Its `get` command refuses to overwrite local evidence.
- `scripts/nested-gvisor-probe.py` creates a fresh OCI bundle and retains
  command, output, runtime logs, and the exit receipt. Its EROFS input must
  be the filesystem image, not an executable such as `dump.erofs`.
- `sources/nested-signal-test.c` reproduces GS and signal-frame incompatibility;
  use `--no-gs` to isolate FP copying and `--bound-fp-copy` to respect the frame.
- `sources/nested-cuda-test.c` launches a real GPU kernel and checks the result.
- `notes/nested-systrap-trace.patch` is diagnostic instrumentation only.
  It must never be included in a published engine. Its dedicated tracing
  goroutine allows the caller to enqueue the first guest context.
- `scripts/build-nested-gvisor-diagnostic.py` retains a source tree and Bazel
  cache for iteration. Source edits between iterations are experimental.
  Release candidates must use the normal `build-runtime-release.py` path
  with the exact cumulative patch for reproducible provenance.

Current lab evidence is on L40S worker `sb-cUFlXNgm3uqY9YzA3t0zOZ`, under
`/home/sw/probe-fault` (shell), `/home/sw/probe-gpu4` (NVML), and
`/home/sw/probe-cuda` (CUDA attempt). The standard build completed on CPU VM
`sb-9PWYVJijEbtrCh8q2MBmqr` with receipt `/home/sw/build-result4.json`;
its assets are `/home/sw/rbuild4/assets/built-44bc13c1a17d4837a2dec393`.
Candidate release 2026.10.24.92 is packaged in `/home/sw/release4` on both
workers. Its archive SHA-256 is
`8cdfefd0064c6cb4d5401372ece3b036cd0a125690516a1f351ba0af8760b687`.
The GPU worker serves it on localhost:8778 with the installed SDK pinned to it.
These worker paths expire; collect the evidence before handoff.

## Later results in this attempt

The engine changes are committed in `sources/gvisor` at
`300909a0815a2f76af6b091f711c2c0e810a4db3`. The native pgalloc and systrap unit
targets pass, as does the targeted read-only mount-hint regression.

The immutable GPU bind now explicitly declares read-only access in its mount
hint. The engine avoids creating a self-overlay for a bind whose hint and
container are both read-only. CUDA and NVML pass with this policy and ordinary
shared caching on the other mounts. Private backing files are truncated when
destroyed, avoiding the unsupported hole-punch operation during teardown.
Partial decommit remains unsupported by the outer host; upstream's manual
zeroing preserves contents but does not reclaim those pages until teardown.

The first full SDK installation reached apt but failed signature validation.
The network helper misdetected SO_PEEK_OFF: Modal accepts setsockopt on an
unconnected TCP socket, but getsockopt returns ENOPROTOOPT. Connected sockets
reject the option too. The helper now requires matching readback before enabling
that capability. The formerly corrupt Ubuntu InRelease download now has SHA-256
`c14060cd8c6d625874dfcb9523a35a395bf4865c28b6b6a82569ef326fe92dc6` inside the guest,
matching the host, and gpgv verifies Ubuntu's signature. The patched helper's
native regression executable covers unsupported and mismatched readback.

Candidate 2026.10.24.93 is the clean standard build with these fixes. Archive
SHA-256: `9e331aa950f38f9b823e083384b0bb5a36f939174f3d5f4f493b13aea7f7e245`.
CPU coding assets: `/home/sw/rbuild5/assets/built-bffbd9eb125749479e71a8b4`.
The CPU also completed preparation of the actual `vr/opensaber` template at
`/home/sw/rbuild5/assets/built-fa730051d6224102a0236ad3`.
Its receipt is `/home/sw/vr-build5.json`. The GPU SDK attempt uses
`scripts/nested-sandweave-check.py`, without the old probe's platform.release
override. Its log is `/home/sw/sdk-coding5.log`.

The current Python sources were verified on the Linux CPU worker:
99 passed, one skipped, and all nine GPU inventory tests passed.
Logs: `/home/sw/sdk-tests5.log` and `/home/sw/engine-tests2.log`.
The same Python suites had first been attempted on macOS: 79 passed,
20 failed, one skipped; most failures were the Linux x86-64 platform check.

## First candidate's SDK blocker

The unmodified SDK startup flow with candidate .93 downloads and installs
packages successfully, then stalls in `/usr/bin/perl /usr/sbin/adduser`
during systemd package configuration. The guest task is 657. Its kernel
goroutine is blocked in
`MemoryManager.HandleUserFault -> mapASLocked -> subprocess.MapFile ->
syscallThread.syscall -> futexWaitWake`, servicing host syscall 9 (mmap).
The watchdog identifies the stuck task. The completion protocol was subsequently
diagnosed and fixed as described below.

The 600-second SDK check expired without a ready sandbox. Its retained
workspace is `/home/sw/sandweave/assets/.build-udqcvfli`; guest name
`install-582328fde9a9`; control directory
`/tmp/sw-build-2yzayz67/gvisor/state`. The diagnostic stack dump is
`/home/sw/installer-stacks5.txt`.

Open Saber has been prepared on the CPU, but no nested VR rendering or
application FPS has been verified. Its 1.8 GiB archive transfer through a
single filesystem read did not finish; a separate bounded-chunk transfer
retains per-chunk checksums. Neither preparation nor transport is VR
acceptance. `nested-opensaber-check.py` records both eyes and collects Monado
application-submission metrics if the actual SDK game launch succeeds.

The experimental branch is **not ready for a public runtime/SDK release**.

## Continuation: explicit syscall completion (2026-10-25)

The user extended the investigation by two hours, starting at approximately
00:01 UTC. A separate diagnostic engine attached to the blocked syscall worker
and captured its registers. The parent was waiting for request 1244's reply;
the worker had returned the requested mmap address and was waiting for request
1245. Its completed reply had no persistent completion marker, and the parent
had missed the wake.

Engine commit `cc41508b33c307dc869ff0e2afc0e62551f291d4` publishes a reply sequence
after the syscall return value and waits for that sequence, tolerating an early,
interrupted or unrelated futex wake. Both amd64 and arm64 assembly implement the
protocol; arm64 uses release/acquire ordering. Host futex errors propagate to
the caller. No timeout or polling workaround is in the production engine.

Validation so far:

- Native pgalloc and systrap targets passed, including a new regression that
  forces the actual stub to publish 1,000 replies before the parent waits.
- The read-only mount-hint regression passed. Arm64 assembly also assembled
  successfully; it has not been executed on an arm64 host.
- The original nested Perl workload now completes 2,000 interpreter launches
  with exit zero in 20.7935 seconds on the L40S. Receipt:
  `/home/sw/probe-perl-fixed1/result.json`; stdout has exactly 2,000 lines.
- The full coding-image package installation on the L40S now finishes in
  71 seconds, including the formerly stuck systemd/adduser step.

Candidate `2026.10.25.94` was rebuilt through the standard release builder,
with engine patch SHA-256
`8b76eac7166dd5a1fea93a0d1d862bbfb502f2fcd36eccd1e22ebcea6cf4a551`.
Its archive SHA-256 is
`ee20ca5f678429db81e097089e1912c6bfe4edef8cc8421dc44381e53cd5a78c`
(64,618,891 bytes). It is served on the L40S at localhost:8780 to an isolated
SDK environment `/home/sw/venv6`, using `/home/sw/sandweave6` as its home.
Full SDK worker and VR acceptance are still in progress.

The full SDK now boots successfully on the L40S. A separate launcher bug was
`StopIteration` while reading `Umask:` from `/proc/self/status`; Modal omits that
field. The private worker now samples its mask before starting threads and
tracks Python mask changes under a lock. Launches inherit that state without
temporarily changing the mask or reading the missing proc field. All 14 native
launcher contract tests pass, including concurrent file creation and launches
with a status file lacking `Umask`.

The tested SDK code is an isolated copy at `/home/sw/sdk7/sandweave`, using
`/home/sw/venv6/bin/python` for dependencies and `/home/sw/sandweave7` as its home
(`SANDWEAVE_LOCAL_DIR=/tmp/sandweave-local7`). Existing workers retain their
original code. `/home/sw/sdk-coding7b.log` records a ready sandbox in 1.3117 s,
successful uname/id/Python commands, and clean closure in 1.9566 s. The earlier
check script mistakenly used `exit_code` rather than `CommandResult.returncode`;
that test-script error was corrected.

The full Open Saber run is now active with candidate .94:
`/home/sw/nested-opensaber8`, log `/home/sw/opensaber8.log`, code
`/home/sw/payload7/nested-opensaber-check.py`. It requests 120 seconds of input
and stereo capture, using the original CPU-prepared VR assets while the normal
SDK importer upgrades their engine. Rendering/FPS are not yet verified.
The active local tool shell is `59f88e`. Investigation extension ends around
02:01 UTC on October 25.

The lab launcher now uses an accessible working directory and removes Modal's
inherited `/root` Python module path when switching to `sw`. A separate test
environment initially lacked SDK dependencies because it was installed with
`--no-deps`; that installation is being corrected with the normal SDK extras.

`notes/nested-syscall-stall-trace.patch` is the diagnostic-only patch against
engine commit `300909a`; it is not part of candidate .94. The original diagnostic
run remains preserved at `/home/sw/probe-perl-stall1`. Native build/test logs
are `/home/sw/futex-fix-build.log`; standard build receipt is
`/home/sw/build-result6.json` on the CPU worker.

## Saved handoff

Draft PR: https://github.com/Pranjal2041/sandweave/pull/2.
The `games` branch has also been created. The engine checkout is clean at
`300909a0815a2f76af6b091f711c2c0e810a4db3`.

The following copies are retained locally under the checkout's existing
artifact ignore rules, independently of the expiring workers:

- `runs/nested-modal-20261024/gpu-evidence5.tgz`: CUDA stdout, engine logs,
  SDK installation logs, network verification and the stalled-task stack dump.
  SHA-256 `ff981ec926bc2e5e7aff5d7eeeac201000267fa4584c0467417e13d8ebeb0da5`.
- `runs/nested-modal-20261024/cpu-evidence5.tgz`: build receipts and native
  test/build logs. SHA-256
  `1c53ca027da38318c54533449f74a5c0c0767de05aae0a009829daa8f2c3953a`.
- `checkpoints/nested-modal-20261024/gvisor.bundle`: verified complete engine
  Git history, including the experimental commit.
- `checkpoints/nested-modal-20261024/sandweave-runtime-2026.10.24.93-linux-x86_64.tar.gz`:
  the 64,617,804-byte candidate archive, verified against the SHA-256 above.

Worker identities for the retained remote paths: CPU
`sb-9PWYVJijEbtrCh8q2MBmqr`; L40S `sb-cUFlXNgm3uqY9YzA3t0zOZ`.
The large VR asset copies remain separate from the verified local evidence;
their completion does not establish that Open Saber ran.

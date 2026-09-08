# Fresh allocation VR investigation

September 8, 2026. The earlier L40S allocation `10333558` ended before this
investigation. Existing evidence establishes Open Saber gameplay and paired-eye
capture, Alyx XR/renderer startup, and the AMS2 Reiza splash. It does not establish
Alyx or AMS2 gameplay. Both game imports are now complete by manifest size and
guest access checks; the new investigation resumes with those assets available.

## Initial allocation and hardware difference

Job `10361037` initially requested one L40S for three hours in `preempt_qos`.
After queueing, the request was broadened to other graphics-capable models and
started on `babel-l9-20` at 19:23:54 UTC. This introduced a hardware variable:
RTX PRO 6000 Blackwell Server Edition, driver 610.43.02, physical device minor 2,
UUID `GPU-5d583653-cd9c-ffe4-7a0f-800b9f757e51`.

`vr-racing-preempt-01` cold-restored `vr-racing-before-signal-fix` with candidate
engine `3494ee1` (immutable build `ef5a71b09d4cc7c2c1d34dcaf4811f3892c90ea65e0973a0c7093e498f3436bd`).
The launch, desktop and ordinary guest exec worked. The CUDA/NVML driver probe
passed as guest UID 1000, and `/dev/kvm` was absent. Monado failed at Vulkan
device creation with `VK_ERROR_INITIALIZATION_FAILED`, before starting a game.
A separate Vulkan information probe failed at the same stage. VirtualGL's
OpenGL probe reported an incomplete framebuffer (0x8cdd).

Nvproxy logged an undefined control `0x90010b`, parameter size 4. Matching local
NVIDIA 610.43.02 source names this `NV0090_CTRL_CMD_SET_LG_SECTOR_PROMOTION`.
That warning is an investigation lead, not an established cause. The runtime
remained responsive. No native graphics comparison or completed comparison to
the default engine was collected on this GPU, so neither hardware support nor
the candidate is isolated as the cause.

A second cold restore, `vr-racing-preempt-control`, booted using the unchanged
default build `19dfb215bc34b326d948e9ca54e7d8a5d2549e1e016aa008ae265cd67293e657`.
The user then requested L40S specifically. The Blackwell job was cancelled at
19:38:34 UTC and Slurm reported its extern step completed at 19:38:36 UTC.
No graphics test was run in that control before cancellation.

Logs remain under `runs/racing/preempt-10361037/` and both environments'
`runs/gvisor/` directories. `gpu-controls-corrected.log` is the valid driver
probe rerun: the initial diagnostic incorrectly injected library-path variables
and selected a staged Vulkan manifest with an unsuitable guest library path.

## Harness changes

`run-gvisor.py --runtime-build` selects a verified immutable build per launch,
without changing the shared default. Both cold restores above exercised this
path. Four runtime-store tests cover selection, default preservation, altered
binaries and paths escaping the build store. Nine environment tests also pass;
a CLI check rejects replacing a live snapshot's runtime before detaching.

The game probes now bound host-side guest-exec waits as well as guest commands.
Alyx joins AMS2 in using a transient systemd unit to contain its dedicated Wine
server and game children. These limits bound the caller's wait; they cannot
force a host NVIDIA kernel thread out of uninterruptible sleep. Game-unit live
acceptance is recorded below. The default remains 45 seconds; explicit timeouts
up to 1,800 seconds allow longer level-loading and interaction probes.

## L40S replacement

Job `10361186` requests exactly one L40S, 8 CPUs and 64 GiB for three hours
(with a two-hour minimum available for backfill),
using account `swelleck`, partition `preempt`, QoS `preempt_qos`, and
`exec sleep infinity`. The previously stalled node `babel-u5-28` is excluded.
The request remains restricted to L40S.

## Concurrent RTX investigation

The user subsequently authorized additional GPUs in parallel while preserving
all unrelated jobs and desktops. Job `10361287` started on `babel-z5-20` at
19:46:55 UTC, with one RTX PRO 6000, device minor 5, driver 610.43.02, and UUID
`GPU-29cef0fc-32f6-57a7-6422-15730951b063`. Its three-hour allocation runs
alongside the pending L40S request.

The first default-runtime control failed during filesystem import, before
starting any GPU application. `finish_boot` had a 300-second readiness loop,
but one 10-second `runsc read` timeout escaped the loop and terminated the
runtime. It now retries this read-only readiness check within the existing
overall deadline. Two regression tests verify retry/release ordering and the
remaining deadline; all seven filesystem-snapshot tests pass. The second cold
restore, `vr-racing-rtx-default-02`, completed and answered guest exec and the
CUDA/NVML probe. Evidence includes `filesystem-restore-error.txt` for the first
attempt and `filesystem-ready.json` for the second.

With the previously selected runtime `19dfb215...`, headless `vulkaninfo
--summary` and Monado both fail at `vkCreateDevice`, with the same undefined
`0x90010b` control warning. The same headless Vulkan probe succeeds natively
in a private Apptainer root made from the base image and filesystem snapshot,
using the same staged NVIDIA libraries on the same allocated GPU. Therefore
the scheduler-aware ioctl candidate is not the sole cause of the RTX failure.
The initial windowed Vulkan probes (native and guest) stopped earlier at
Xvnc presentation, so the headless results are the relevant device-creation
comparison. Logs: `runs/racing/preempt-10361287/default-vulkan-headless.log`,
`default-monado.log` and `native-headless-app.log`.

Native extraction skipped device-node creation because the host user is
unprivileged. Apptainer supplies its isolated `/dev` and the explicitly allocated
GPU devices; the skipped Docker device node is unrelated to the Vulkan control.
Only new disposable directories on this node were created.

## Blackwell graphics control repair

Engine commit `c2f78eb` adds `NV0090_CTRL_CMD_SET_LG_SECTOR_PROMOTION` to the
610.43.02 graphics control table. The matching driver header
`src/common/sdk/nvidia/inc/ctrl/ctrl0090.h` declares one enum parameter,
`promoType`, with no embedded pointers. The existing simple-control handler
copies the parameter and invokes the driver's normal validation. The command
is gated on the graphics capability. Its ABI information entry is also recorded.

The full five-binary build and nvproxy tests pass. Candidate build
`d7c9c0e88768628740fb5c19f1e44a5e90c3bbb72fc56ee9d20a7ad0d42acdf7` cold-booted
`vr-racing-rtx-sector-01` on the same RTX GPU as the failing control. Headless
Vulkan now exits 0, Monado initializes, and Open Saber renders on Xvnc and in
paired-eye captures. A controller-trigger input started the Time Lapse level;
the later paired-eye screenshot visibly shows gameplay and a score of 140.
This comparison identifies the rejected sector-promotion control as a startup
blocker on this Blackwell GPU. The runtime also contains the earlier
scheduler-aware ioctl change; recovery from the previous host driver wait is
still untested.

In a 14.992-second gameplay window, Monado recorded 1,348 application frame
submissions (**89.85/s**) at a 90 Hz target, with median/p95/p99 intervals
11.12/12.63/14.83 ms. The compositor's reuse fraction was 4.16%. This is an
OpenXR submission measurement, not physical-headset scanout timing. Opened and
inspected `open-saber-desktop.png`, `open-saber-eyes.png`,
`open-saber-after-play.png` and `open-saber-play-later.png` under
`runs/racing/preempt-10361287/`. The last is gameplay; the immediate post-input
capture still showed the menu. Measurements: `open-saber-metrics.json`.

The default runtime selector remains `19dfb215...`; candidate launches specify
their immutable build explicitly. The older control sandbox remains available
for comparison. Subsequent game probes stop only this test's Open Saber service,
keeping its Monado instance for the new game.

## Game startup with complete imports

AMS2 on the RTX passed the earlier splash and rendered a sign-in error in the
desktop and both eyes: **Steam is not running**. Opened and inspected
`ams2-at-start.png`, `ams2-at-90.png` and `ams2-eyes-at-90.png` in the RTX run
directory. No driving is established. The transient unit ended at its requested
180-second limit (reported runtime 3min 2.234s), returned a timeout result and
reported no Wine crash. The runtime remained responsive; the unit was inactive
before the next game started. This establishes a Steam-client requirement for
the next stage, not a graphics failure.

Alyx now loads the `startup` map and reaches its stereo main menu, including
tracked hands. The earlier missing/corrupted-file dialog is absent. The first
240-second probe ended at its limit (4min 2.024s) while still at the menu.
A second probe uses a 900-second limit for interaction. Controller pose changes
visibly move the hand; a trigger selected the Addons menu. Menu pointer
calibration and level entry are still under investigation. The 14.977-second
main-menu sample recorded 647 application submissions, **43.13/s**; this is not
a gameplay measurement. Evidence includes `alyx-eyes-startup.png`,
`alyx-02-continue-positive-offset.png`, `alyx-menu-metrics.json` and the
`alyx-run-01.log`/`alyx-run-02.log` probe logs. Both games used the explicit
experimental Primus-VK API-version option; this is not conformance validation.

## L40S live control

Job `10361186` started on `babel-o9-20` at 20:04:29 UTC, ending at 23:04:29 UTC.
Its allocated L40S is device minor 0, driver 610.43.02, UUID
`GPU-91d03de6-3e87-becf-51ed-2693e3de06fb`. SSH reported a changed host key; the
new ED25519 public key was independently read through the authenticated Slurm
allocation and its fingerprint matched. Subsequent SSH uses the scoped
`runs/ssh-known-hosts-10361186` file; the user's known_hosts was not changed.

`vr-racing-l40s-01` cold-restored the same snapshot with candidate `d7c9c0e...`,
32 GiB guest memory and eight guest CPUs. Open Saber gameplay and two successive
stereo views were opened and inspected (`open-saber-after-input.png` and
`open-saber-now.png` under `runs/racing/preempt-10361186/`). A 14.988-second
window recorded 1,019 application submissions, **67.92/s**, at a 90 Hz target.
These separate allocations differ in CPU hardware and load; this is not a
controlled comparison of GPU speed. A fresh Alyx probe is now running there.

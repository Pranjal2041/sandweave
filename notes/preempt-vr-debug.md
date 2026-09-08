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
acceptance on the new allocation remains pending.

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

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

Alyx now accepts explicit `--dxvk-config`, `--wine-debug`, and `--map` diagnostic
options. Long-running commands stream through a pipe copied by the parent,
preserving logs as they arrive instead of buffering the entire run until exit.
Small readiness and preparation RPCs still capture their bounded output.
The first streaming attempt passed a regular file directly to guest exec; the
guest's independent imported-file offset caused the final JSON summary to
replace the beginning of run 05's log. Its subsequent exception trace remains
available, but the original leading bytes were not preserved. The pipe repair
passes incremental-output/order and timeout regressions, and run 07 preserves
both its initial `Running as unit` line and final JSON summary in order.

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
`alyx-run-01.log`. The second probe's buffered log was not flushed before the
node failure described below; its empty `alyx-run-02.log` is not completion
evidence. Both games used the explicit
experimental Primus-VK API-version option; this is not conformance validation.

### RTX node failure

Slurm marked job `10361287` **NODE_FAIL** at 20:23:46 UTC, naming
`babel-z5-20` as the failed node. The last successful guest/capture operations
were at approximately 20:18:41 UTC; the node subsequently stopped answering
SSH and Slurm steps. Its state included `NOT_RESPONDING`. Shared run artifacts
remain available, but the test environments on that node are no longer
accessible. No host/kernel diagnostic after the failure was available, so the
cause is unknown. This is not evidence that the earlier driver-wait problem
was repaired or that this experiment caused the node failure. No host reset or
cancellation of another user's job was attempted.

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
controlled comparison of GPU speed.

Alyx also reached its stereo main menu on the L40S. Controller input accepted
the performance prompt, selected Continue, and accepted the load-save dialog.
The console then reported restoration of `s0/autosave` and spawning
`a1_intro_world_2` at 20:31:02 UTC. The first 900-second probe expired at
20:32:20 UTC while level loading still showed a black stereo frame. Its unit
ended with the expected timeout and the runtime remained responsive. A second
1,800-second probe reproduced the same menu interaction, then exited 139 at
20:35:00 UTC during level loading, after 2min 20.836s. Wine reported
`_wassert (!status && "vkCreateGraphicsPipelines")` in
`winevulkan/loader_thunks.c:3095`. This is a game-process failure before the
probe deadline. Ordinary guest exec and Monado remained responsive; a host
thread check found no uninterruptible Sentry thread. The post-exit stack dump
is not a stack trace of the failing game. No Alyx level gameplay is established.

An attempted `dxvk.conf` in the game directory also exited 139, but its log did
not confirm reading that file; it is not a valid configuration comparison. The
test-created file was removed after verifying its exact content. A subsequent
run used `--dxvk-config 'dxvk.enableGraphicsPipelineLibrary = False'` and
`--wine-debug=-all,err+all,+seh`. DXVK explicitly logged that effective setting.
This run loaded further resources, then exited 139 after 1min 57.438s. The
first traced access violation was a write at `0x1c69a0000` from
`0x6ffffa2f96e7`, followed by repeated faults during Wine exception unwinding.
The next run's module-load addresses identify this code region as
`animationsystem.dll`; disassembly of offset `0x1b96e7` is `rep movsb`.
This does not establish the cause of the invalid access. Trace and pre-failure
memory mappings are in `alyx-run-04-no-gpl-seh.log` and `alyx-04-maps.log`.
The DXVK diagnostic setting is described in the upstream
[v2.5.3 configuration](https://github.com/doitsujin/dxvk/blob/v2.5.3/dxvk.conf).

For this head pose/menu layout, the working right-controller position is
`0 1.3 -0.25`, with `--aim-pitch-offset 55`. Empirically calibrated targets:

| Action | `--aim-at` |
| --- | --- |
| Performance prompt Accept | `0.09 1.37 -0.73` |
| Continue | `-0.065 1.43 -0.68` |
| Load-save Accept | `0.09 1.39 -0.73` |

Use `vr-lab.py NAME input -- --device right ... --click trigger --hold 0.4`.
These coordinates are specific to the observed menu arrangement, not a
general Alyx interaction mapping. Opened and inspected evidence includes
`alyx-accept-near-plane.png`, `alyx-continue-adjusted.png`,
`alyx-load-accepted.png`, `alyx-after-load.png` and `alyx-load-later.png` in
`runs/racing/preempt-10361186/`.

## Level-loading controls and host mapping pressure

Run 05 selected Start New Game, Chapter 1, and Start Game without addons, then
accepted creation of the private guest's new slot S2. It spawned
`a1_intro_world`, rather than restoring the imported save. It failed with the
same `animationsystem.dll+0x1b96e7` write fault and recursive Wine unwind faults,
exiting 139 after 3min 23.291s. The imported save alone therefore does not
explain the failure. Runs 06 and 07 use `--map a1_intro_world` to reproduce
loading without menu navigation; both exited 139 before their deadlines.

A private native Apptainer comparison uses the same base image, prepared
filesystem, game asset binds, GE-Proton9-27, DXVK override, Primus manifest
experiment, Monado and L40S. It reached the stereo main menu and accepted the
same Continue/Accept inputs, then failed loading `a1_intro_world_2`. Wine logged
many `mmap() error Cannot allocate memory` messages and an assertion in
`ntdll/unix/virtual.c:create_view`. Direct native loads of `a1_intro_world` also
failed with mapping-allocation errors. This is not a successful native game
control, nor proof of a gVisor-only game bug.

`scripts/native-alyx-control.sh` runs in the private session made by
`run-native-gpu-app.py`, with an isolated Xvnc, Monado socket/config, input port,
and a 900-second game limit. Its cleanup stops only that native Wine prefix
and Monado child. Use `--foreground` inside a Slurm step; the initial detached
attempt ended with its step before application startup. Bind the private
root's entire `home` at `/home`, since Apptainer containment otherwise hides
`/home/ga`. Native attempts 01 and 02 did not reach application startup; 03
through 06 are the valid game controls. Native logs were copied into the shared
run directory before handoff.

The host's `/proc/sys/vm/max_map_count` is **65,530**. During sandbox run 07,
`host-vma-probe.py` observed **65,415** mappings in a systrap application host
process immediately before failure. The native game rapidly reached tens of
thousands of mappings (sampled peaks 62,122 and 60,021 in runs 05 and 06), then
reported `mmap` allocation failures. Sampling missed the native instantaneous
peak. This strongly indicates the host mapping limit as a common blocker; a
successful control with the limit relieved has not been run. The Linux
[max_map_count documentation](https://docs.kernel.org/admin-guide/sysctl/vm.html#max-map-count)
describes this per-process limit. The guest reports a synthetic value of
2,147,483,647, which does not remove the host's limit on systrap processes.
No host sysctl, sudo operation, or unrelated job was changed.

`host-vma-probe.py PID --seconds 50` samples only the specified owned process
and its descendants, checks its start time against PID reuse, and stops on
exit. `run-native-gpu-app.py --foreground --vma-log PATH` applies this to its
own native child. Evidence: `alyx-07-host-vmas.jsonl`,
`native-05-vmas.jsonl`, `native-06-vmas.jsonl`, and the corresponding game logs.

Prepared filesystem snapshot `snapshots/vr-l40s-alyx-prepared-10361186` was saved
in 4.198 seconds with the source sandbox resumed. It contains the prepared
Wine prefix and writable configuration; assets remain external read-only
binds. Full verification passed on the original node in 8.065 seconds. The
automatic verifier had remained pending after its Slurm step ended; a first
explicit attempt on the login node correctly rejected initial verification
away from the frozen source. The subsequent original-node run checked all
seven files and passed. It does not preserve a live GPU context or a running game. Native
extraction exercised its file contents; a new gVisor cold restore of this
particular snapshot has not yet been tested. Unprivileged native extraction
skipped special device nodes, and the overlay tar reported only the expected
Docker `backingFsBlockDev` mknod failure. Native Apptainer supplies its own
isolated `/dev` and the allocated GPU binds.

## Handoff

The L40S allocation `10361186` remains running on `babel-o9-20`, scheduled until
23:04:29 UTC (19:04:29 EDT), subject to preemption. Sandbox
`vr-racing-l40s-01` uses VNC port **60689**. Open Saber was restarted and its
paired-eye gameplay was inspected again after the Alyx/native controls. Alyx
probe units and native test applications have exited; Monado and Open Saber
are the remaining test applications. All unrelated jobs and existing desktops
were left in place.

From this lab checkout, the verified node-specific SSH key file can be used as:

```sh
ssh -o UserKnownHostsFile="$PWD/runs/ssh-known-hosts-10361186" \
    -o StrictHostKeyChecking=yes babel-o9-20
```

GPU operations on the node use `SLURM_JOB_GPUS=0` with `SLURM_STEP_GPUS` unset.
The default runtime selector remains unchanged. The new runtime is an explicit
candidate, with tested Open Saber gameplay, game-menu acceptance, and the
remaining level-loading limitations documented above.

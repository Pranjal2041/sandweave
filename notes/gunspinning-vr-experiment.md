# GunSpinning VR: native Linux gamepad and OpenVR controls

Tested 2026-09-08 (America/New_York), Slurm job **10364024**, L40S on
`babel-m5-28`. Both modes run the official, unmodified **Linux 2.0.1** game
inside the no-KVM gVisor sandbox. No Steam client, Wine, Windows installation,
login or payment was used. [The developer lists Linux gamepad and SteamVR
controller support](https://demonixis.itch.io/gunspinning-vr).

## Accepted behavior

- **Gamepad / flat screen:** A selects Play, the left stick selects Training,
  A fires (HUD ammunition 6 → 5), right-stick axes change yaw and pitch, and
  B reloads (5 → 6). The final neutral state does not drift over the recorded
  two-second check. The town story
  scene also loaded during exploration. The recorded control sequences use
  the bottle range. Analog axes have compatibility limitations described below.
- **OpenVR / tracked controls:** xrizer v0.5 translates this native game's OpenVR
  calls to OpenXR, using the existing patched Monado compositor and its virtual
  headset/Index controllers. A tracked aiming ray and trigger select Play and
  Training. Controller orientation moves the gun; three trigger presses change
  its visible ammunition from six rounds to three. Pressing the thumbstick
  while holding it down reloads. Head translation changes the stereo view.
- **Offline launch:** a fresh disposable guest, `vr-gunspin-offline01`, was
  created from the existing prepared VR snapshot with `--network-policy offline`.
  Both modes launched and accepted their recorded controls there. This used
  staged local game files and the lab's packet-dropping network policy, with
  incoming desktop/control forwards retained. Initial asset acquisition requires
  downloading. The earlier suspended-relay experiment is recorded below and is
  superseded by this test. A final guest TCP probe to `1.1.1.1:443` failed after
  its two-second timeout; the saved policy has no allowed egress CIDRs.

This acceptance covers scene loading and the control loop. It does **not**
claim completing all eight levels, verified bottle hits/score attribution,
physical headset tracking or scanout, sound, haptics, left-hand gameplay actions,
or prolonged stability.
The currently tested configuration uses `-noaudio`; FMOD/spatializer warnings
remain. The virtual gamepad has no physical USB/evdev device.

## Execution paths

```text
Unprivileged Slurm allocation / Apptainer, /dev/kvm absent
  gVisor systrap + existing nvproxy → allocated NVIDIA L40S
    Native Unity 2019.2.1f1 / Vulkan
      Gamepad mode: SDL_DYNAMIC_API joystick proxy → Unity / InControl
      VR mode: OpenVR → xrizer v0.5 → OpenXR → patched Monado debug_image
                 Monado remote driver ← head / left / right poses and controls
      Desktop window: Primus-VK NVIDIA rendering → llvmpipe presentation → Xvnc
```

llvmpipe handles the desktop presentation side of Primus-VK. The game render
GPU is NVIDIA L40S, as recorded by Primus-VK. Monado performs real Vulkan
composition into offscreen images. Xvnc and Monado retain separate roles.
The experimental Wayland option was not used or changed.

The selected immutable runtime is
`d7c9c0e88768628740fb5c19f1e44a5e90c3bbb72fc56ee9d20a7ad0d42acdf7`
(runsc SHA256 `680408c88b46625debdafdaa4cef2784705ee42cc2689ed703707792f234a5e5`).
No gVisor source edits or shared runtime-default changes were needed. Engine
checkout HEAD remains `c2f78eb0077f8394917b9bcdece2e521e3150d76`.
Each guest advertises eight CPUs, with weighted sharing of the same allocated
8-CPU pool, a 12 GiB guest-page budget and separate 2 GiB runtime guard.
GPU device is `/dev/nvidia4`, driver 610.43.02. These are shared resources, not
GPU partitions or independent CPU reservations.
The snapshot preserves `guest_gs=true` and cgroup v1; runtime debug logging
was disabled for these launches.

## Gameplay performance

The final offline-policy guest's **10-second** window during the VR bottle-range
control sequence measured **61.02 application submissions/s** on L40S with the
flat game stopped. The median frame interval was 14.56 ms, p95 31.32 ms,
p99 35.62 ms and maximum 142.78 ms. Monado was configured for 90 Hz and reused
an application frame on 32.1% of its uses. This establishes live gameplay
near 61 FPS on average, with uneven frame pacing,
not a steady 90 FPS or a motion-to-photon result.

A preceding verified VR-only sequence measured 61.05 FPS (p95 30.81 ms,
maximum 52.02 ms). A separate sequence with the flat game running in the other guest
measured 36.92 FPS (p95 40.37 ms). Both guests share the same eight host CPUs
and GPU. This exploratory comparison is not a controlled benchmark isolating
one bottleneck. These windows include intermittent eye evidence captures and
continuous Xvnc desktop presentation. No native gamepad-mode FPS was measured.
[The machine-readable measurements](gunspinning-vr-measurements.json) preserve
frame counts, distributions, reuse fractions and metric-file hashes.

## Compatibility changes and failed checks

1. **Vulkan-only shipped shaders.** Forcing OpenGL fails because the Linux
   archive was built without that shader set. Vulkan is required for this build.
2. **Primus-VK image padding.** Upstream Primus-VK inferred image height from
   allocation size divided by row pitch and rejected different NVIDIA/Mesa
   padding. GNOME maximized the requested 1280×720 window to a 1280×731 client
   area, reproducing `Layouts don't match at all`. The explicit
   [patch](primus-vk-visible-rows.patch) copies the actual swapchain height,
   validates layout capacity and respects the two row pitches. Tested at the
   actual 731-row client size. No game shader or assembly was altered.
3. **Userspace gamepad.** SDL's dynamic API permits an opt-in replacement table.
   The [proxy](../scripts/sdl-gamepad-proxy.c) first obtains the player's own SDL
   implementations, then replaces joystick enumeration, getters and event
   generation. Unity additionally required an SDL joystick-added event before
   opening the device. InControl selected GenericLinuxProfile. The pinned SDL
   header defines function slot indices; `-Bsymbolic` prevents the proxy entry
   from being interposed by Unity's original symbol. This has been qualified
   only against this x86-64 Unity player, not arbitrary SDL applications.
4. **Input transport.** A 48-byte, atomically replaced state file carries eight
   axes and fifteen buttons with a monotonic expiry. The helper returns to
   neutral on completion; expired input also becomes neutral in the proxy.
   The qualified neutral state is zero on every axis. Analog trigger calibration
   remains unresolved: raw axis 4 fired with one negative-resting-value setup,
   but that setup also caused continuous camera drift. It is not an accepted
   mapping. A/B are the qualified fire/reload controls. Unity/InControl's generic
   profile and the game's additional axis handling need care: the final
   positive full-scale pulses establish raw axis 3 as horizontal aim and raw
   axis 4 as vertical aim. The helper provides `rx`/`ry` aliases for those two,
   `lx`/`ly` for menu axes 0/1, and `axis0` through `axis7` for raw probes.
   Small 0.2 pulses did not visibly aim in the final checks; the recorded
   acceptance uses 1.0 for 0.15 seconds. Trigger axes are not assigned semantic
   names. The earlier `rx=2` calibration was rejected: it animated the guns
   without changing the camera view.
   Native gamepad input does not substitute keyboard/mouse gameplay actions.
   A desktop click was used initially to focus the window/leave GNOME overview.
5. **Stale VR submissions.** An early session submitted old eye textures at
   about 84 FPS while its main thread was unresponsive. Head motion produced
   compositor reprojection, without new game simulation or controller motion.
   That number is **excluded from gameplay performance**. Restarting with the
   Primus padding fix and `-noaudio` produced live pose/button/scene changes.
   Those two changes were introduced together; the stall's individual cause
   has not been isolated.
6. **Recorder timing and file permissions.** Sending the menu sequence three
   seconds after launch could precede input readiness. The saved sequences now
   allow fifteen seconds, then twelve seconds for the training introduction.
   A cold guest's first launch can take longer; wait for a visible menu and
   leave GNOME overview before starting the sequence. Always inspect the images:
   file names such as `fired.png` do not prove a shot. Node tar extraction under
   umask 027 also removed other-user access to an uploaded proxy. Preparation
   now installs the library inside the guest
   with mode 755 and checks loading as user `ga`.

The initial offline probe suspended the complete Ethernet transport. After an
extended interval, guest execution/control calls began timing out; resuming the
two relays immediately restored control. Transport backpressure is a plausible
explanation, not an isolated engine diagnosis. This method is not the lab's
supported offline configuration. A fresh guest `vr-gunspin-offline01` uses the
existing `--network-policy offline` packet-dropping policy while retaining
working incoming VNC/SSH forwards. No network-policy implementation was changed.

## Reproduction

Use a disposable GPU guest with the prepared Monado/Xvnc setup described in
[the VR experiment](vr-monado-experiment.md) and
[the Alyx presentation setup](alyx-startup-experiment.md). The tested base was
`snapshots/vr-l40s-alyx-prepared-10361186`. It contains the compiler, Vulkan/Xrandr
headers, `libprimus-vk1`, Mesa Vulkan presentation driver and Monado setup.
Those packages are guest dependencies; installation/building needs no host sudo.
Stage xrizer v0.5 as documented there. Do not run the Open Saber `vr-lab start`
command alongside GunSpinning; start only Monado (`alyx-probe.py NAME monado`).

The offline guest was launched with this command from the staged lab inside an
eight-CPU Slurm step. Device 4 is specific to this allocation; select the
allocated device for a new job. The foreground launcher keeps the guest alive.

```bash
python scripts/run-gvisor.py \
  --restore snapshots/vr-l40s-alyx-prepared-10361186 \
  --runtime-build tools/runtime-builds/d7c9c0e88768628740fb5c19f1e44a5e90c3bbb72fc56ee9d20a7ad0d42acdf7 \
  --gpu 4 --memory-mib 12288 --runtime-memory-mib 2048 --guest-cpus 8 \
  --network-policy offline --no-runtime-debug vr-example
```

Acquire `gunspinning-vr-linux.zip` through the developer's free/name-your-price
itch.io flow. The tested archive is 416,337,171 bytes, SHA256
`85c440f22f16fcbeec018b0f4be4df8bb3b3ac091a3eda83f43cf2c8d06ad392`.
No signed download URLs or download-session tokens are committed.

```bash
python scripts/stage-gunspinning.py --archive downloads/gunspinning-vr/gunspinning-vr-linux.zip
# Stop this guest's game before prepare; it installs/builds the two proxy libraries.
python scripts/gunspinning-probe.py vr-example prepare
python scripts/gunspinning-probe.py vr-example start --mode gamepad
python scripts/gunspinning-sequence.py vr-example gamepad \
  notes/gunspinning-gamepad-sequence.json --output runs/my-gamepad-check
python scripts/gunspinning-probe.py vr-example stop --mode gamepad

python scripts/alyx-probe.py vr-example monado
python scripts/gunspinning-probe.py vr-example start --mode motion
python scripts/gunspinning-sequence.py vr-example motion \
  notes/gunspinning-motion-sequence.json --output runs/my-motion-check
python scripts/vr-metrics.py runs/my-motion-check/gameplay-metrics.pb --tail-seconds 10
```

The recorder requires Pillow on the orchestration host and the existing FastIO
assets for flat screenshots. `prepare` compiles pinned Primus-VK commit
`7076c2e6a55cfc7c292eb68ca17b00dae498ef81` plus the checked-in patch inside the
selected guest, and backs up its original layer manifest. Staged game assets,
source dependencies, compiled libraries, logs and screenshots are ignored;
authored scripts, patches and sequences are committed. Preparation does not
fetch packages. `stage-gunspinning.py` verifies the downloaded archive hashes and uses
the cached dependencies on repeated runs.

For manual input, run inside the guest:

```bash
runuser -u ga -- python3 /opt/gunspinning-lab/gamepad-input.py --button a
runuser -u ga -- python3 /opt/gunspinning-lab/gamepad-input.py --axis rx=1 --hold .15
runuser -u ga -- python3 /opt/gunspinning-lab/gamepad-input.py --axis ry=1 --hold .15
runuser -u ga -- python3 /opt/gunspinning-lab/gamepad-input.py --button b
```

For tracked controls, the saved motion sequence shows pose/trigger commands
and the Monado state update for reload:
`{"right":{"thumbstick":[0,-1],"thumbstick_click":true}}`, followed by the
neutral release. Monado state updates persist until changed. The game-specific
menu aiming pose uses a +50-degree local pitch offset; it is not a generic
coordinate calibration for all games. The legacy pose command sends input
without per-action acknowledgement; visible game changes establish acceptance.

## Allocation and preservation

The allocated node's `/data` automount hung. A selected copy of this lab and
its existing prepared snapshot was staged at `/tmp/gunspinning-lab-10364024`,
with state at `/tmp/gunspinning-state-10364024`. This is disposable runtime and
asset staging; the original repository, transcript, snapshots and user desktops
were preserved. Evidence is copied back to the original workspace before handoff.
No Codex/session data was moved. The separately allocated, unused RTX PRO 6000
job **10364041** was released after the L40S tests.

Node commands use `srun --jobid=10364024 --overlap --chdir=/tmp --export=NONE`
with `/bin/bash --noprofile --norc`, a normal executable PATH,
`APPTAINER_NO_MOUNT=bind-paths`, and `PYTHONNOUSERSITE=1`. These avoid configured
Apptainer binds and user Python startup paths that traverse the broken mount.
Preserve file modes when transferring assets, or restore read/execute access
on this disposable GPU asset tree. Keep the allocation's holding srun steps
alive while using its detached guests. This staging workaround does not repair
the cluster automount.

At handoff, `vr-gunspin-offline01` is left running in the VR menu. Its node-local
VNC port is **46669**, SSH port **58781** on `babel-m5-28`; access uses the lab's
existing tunnel/forward arrangement. The other two test guests retain their
desktops with GunSpinning stopped. Job **10364024** ends at **23:07:22 EDT on
2026-09-08**, unless preempted earlier. Runtime state under `/tmp` is disposable;
the accepted artifacts below are preserved in the original workspace.

## Evidence and validation

### Both-eye video demo

For the distinction between virtual-headset eye views and physical headset
appearance, and the offline partial panorama conversion, see
[projection and capture provenance](vr-offline-equirectangular.md).

Every VR demo now requires videos of both eyes. The follow-up recording captures
the same offline GunSpinning guest through the existing continuous stereo ring:

- [Left-eye video](../runs/gunspinning/gunspinning-stereo-demo-01/frames/left-eye.mp4)
- [Right-eye video](../runs/gunspinning/gunspinning-stereo-demo-01/frames/right-eye.mp4)
- [Synchronized side-by-side video](../runs/gunspinning/gunspinning-stereo-demo-01/frames/gameplay.mp4)

Each MP4 contains **748 frames over 33.668 seconds**, with exactly matching
presentation timestamps. Each eye is 960×1080; the pair is 1920×1080. These
are actual sequential compositor captures, preserving the captured timing.
The source recording averaged **22.19 stereo pairs/s** with a 30/s capture cap;
there were no ring skips or recorder drops. The final ten-second gameplay
window measured 66.37 application submissions/s. Capture cadence and game
submissions measure different stages; neither is physical headset scanout.

All three MP4s were fully decoded, with no decode errors when retaining the
source timestamp precision. Decoded stereo frames at 1, 17, 27 and 30 seconds
and separate eye frames at 21, 24 and 27 seconds were visually inspected.
Both eye videos show three rounds after the shots and six after reloading;
the later sweep moves both hands and the view, with distinct stereo parallax.
The [video manifest](gunspinning-vr-video.json) records hashes, timing checks,
capture statistics and the precise visual acceptance scope. Lossless paired
frames and acknowledged input events remain beside the videos.

`scripts/gunspinning-vr-demo.py` runs the recorded menu, training, firing,
reloading and head/controller sweep. Stop this disposable guest's game and
Monado first; the demo owns its temporary game/runtime and leaves Xvnc alive:

```bash
python scripts/gunspinning-probe.py vr-example stop --mode motion
python scripts/gunspinning-probe.py vr-example exec -- systemctl stop vr-monado-live
python scripts/gunspinning-vr-demo.py vr-example --output runs/my-stereo-demo
```

The orchestration host needs Pillow, zstandard, ffmpeg, protobuf and the generated
`tools/gpu/vr/monado_metrics_pb2.py`. The initial capture completed before a
missing generated metrics module interrupted export. Its lossless frames were
preserved and all three videos were exported from them in the original workspace.
The node dependency is now staged, and the demo checks it before starting.
Existing recordings can also be exported without rerunning gameplay:

```python
from vr_stream import RecordedFrames
recording = RecordedFrames('runs/my-stereo-demo/frames')
recording.video()
recording.video('left')
recording.video('right')
```

The shared stream's default Open Saber launch remains available; the existing
`vr-stream.py --record --video` now exports all three views. Its 12 existing
stream/ownership tests pass. After recording, the ordinary Monado service and
GunSpinning VR menu were restarted in `vr-gunspin-offline01`.

### Screenshot checks and provenance

The [acceptance manifest](gunspinning-acceptance.json) records the exact input
sequences, observations, file hashes, network policy and dependency hashes.
The saved recorded steps match the two checked-in reproduction sequences.
The node's game executable, gameplay assembly and OpenVR library were hashed
and matched against the official zip. The following are local, ignored artifacts:

| Check | Before | After |
|---|---|---|
| Gamepad A fires | [Six rounds](../runs/gunspinning/gunspinning-final-full-axis/ready.png) | [Five rounds](../runs/gunspinning/gunspinning-final-full-axis/fired.png) |
| Gamepad aim and B reload | [Yaw changed](../runs/gunspinning/gunspinning-final-full-axis/aimed.png) | [Six rounds again](../runs/gunspinning/gunspinning-final-full-axis/reloaded.png) |
| Gamepad vertical aim | [Neutral view](../runs/gunspinning/gunspinning-final-full-axis/neutral-stable.png) | [Pitch changed](../runs/gunspinning/gunspinning-final-full-axis/axis4.png) |
| VR trigger and reload | [Three rounds after firing](../runs/gunspinning/gunspinning-policy-offline-motion/fired-three.png) | [Six rounds after reload](../runs/gunspinning/gunspinning-policy-offline-motion/reloaded.png) |
| VR head motion | [Initial stereo view](../runs/gunspinning/gunspinning-policy-offline-motion/ready.png) | [Moved head](../runs/gunspinning/gunspinning-policy-offline-motion/head-moved.png) |

Player/journal logs, the failed public TCP probe and guest status/settings are
under `runs/gunspinning/gunspinning-handoff/`. Earlier failed or superseded
probes remain available under `runs/gunspinning/`; they are not accepted based
on their filenames. The final live menu is saved under
`runs/gunspinning/gunspinning-live-menu/`.

Validation included repeated live launches and the visually inspected control
sequences, compiled Primus-VK in the guest, SDL proxy compilation with
`-Wall -Wextra -Werror`, Python syntax checks, archive/hash checks and the staged
Git diff. No GPU-state snapshot acceptance or broader engine regression is
claimed by these game-specific tests.

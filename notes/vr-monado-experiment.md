# First VR game in the sudoless sandbox

Tested 2026-09-07 (America/New_York), on `babel-u5-28`, Slurm job 10333558.
The project goal is capable sandboxes without host sudo. This experiment is
one interaction workload, not a replacement definition of that goal.

## Result

The published **Open Saber 0.5.0 Linux binary** runs its actual OpenXR mode in
the existing gVisor GPU sandbox. Monado supplies a virtual headset and two
Index-style controllers through its upstream remote driver. A controller pose
and trigger press selected Play, started the bundled Time Lapse level, and
produced moving notes, saber collisions, score and combo changes. The game's
desktop simulator was not used for this acceptance.

Both the Xvnc mirror and a GPU readback of Monado's composed left-eye image
were visually inspected. The independent OpenXR query returned two recommended
views of **1344 × 1512**, sample count 1. The game uses its default render scale
of 1.0. Evidence captures are scaled by Monado's existing readback helper to
960 × 1080 per eye; they do not represent a reduction of the game render size.

This is an **experimental, patched Monado path** with an unmodified game binary.
It establishes VR game execution, GPU interoperation and virtual controller
input. There was no physical headset, physical tracking system, display scanout,
video encoder, network headset stream, or motion-to-photon measurement.

## Layers actually exercised

```text
Unprivileged Slurm allocation / Apptainer
  └─ gVisor systrap + existing nvproxy integration
       ├─ Open Saber / Godot 4.3
       │    ├─ OpenGL through VirtualGL EGL → NVIDIA L40S
       │    └─ OpenXR → Monado client library
       │                    └─ IPC + shared GPU images → Monado Vulkan compositor
       │                                                └─ offscreen GPU images
       ├─ Monado remote driver ← headset/controller poses and buttons
       └─ Xvnc ← optional VirtualGL desktop readback
```

gVisor remains the isolation engine. Xvnc is a display/mirror option. Monado is
the XR runtime and compositor. Wayland remains a separate display option under
investigation; this experiment did not run a Wayland compositor.

The Monado target is the upstream `debug_image` target in the **main rendering
compositor**. It allocates GPU images and runs composition. It is not the null
compositor, and this test does not use `XR_MND_headless`, which would skip the
graphics binding. Its clock-based pacer has no physical vblank feedback.

The sandbox has 4 advertised CPUs, weighted CPU sharing over the inherited
28-CPU Slurm pool, 24 GiB guest-page budget and a separate 1 GiB runtime guard.
It exposes allocated `/dev/nvidia0`: L40S UUID
`GPU-203c2df2-d777-ba3e-e218-dfc79347f5a5`, driver 610.43.02. GPU access is shared;
no GPU partition or exclusive reservation was introduced. `/dev/kvm` is absent
inside the runtime. No host sudo, driver replacement, or admin change was used.
The existing Resolve environments were preserved.

## Measurements

These are 30-second windows from Monado's binary metrics, on the same bundled
level with virtual controllers. Windows cover different portions of the level
on a shared node/GPU; this is an exploratory comparison, not a controlled
performance benchmark. The table reports actual application submissions,
separately from the compositor's configured target.

| Configuration | Compositor target | Application FPS | Frame interval p95 |
|---|---:|---:|---:|
| Initial continuous desktop readback | 90 Hz | 48.77 | 27.63 ms |
| No continuous desktop readback | 90 Hz | 79.73 | 20.36 ms |
| Same, repeated haptic logging removed | 90 Hz | 80.43 | 20.30 ms |
| No continuous desktop readback | 120 Hz | 90.00 | 18.57 ms |
| PBO desktop readback, live Xvnc mirror | 120 Hz | 61.25 | 20.44 ms |

At 120 Hz with readback disabled, 2701 application frames were delivered over
29.999 seconds; 2669 distinct application frames were used in 3595 compositor
frames. About 25.8% of compositor uses reused an application frame. **120 Hz
composition is not 120 FPS game rendering.** The 90 FPS average is not a steady
11.1 ms frame cadence: p99 was 23.07 ms and the maximum interval was 34.92 ms.

For that window, median compositor GPU composition cost was 0.0255 ms. This
measures Monado's GPU work, not the game's entire GPU workload. Runtime wait,
application CPU/render submission, and desktop readback account for much more
of the observed frame interval. Disabling VirtualGL readback improved observed
throughput materially. Removing repeated haptic logs had little measured effect.
The remaining jitter has not been conclusively assigned to a particular
scheduler, driver call or game subsystem.

`vr-metrics.py` also reports runtime wait, wake-to-begin, begin-to-end-frame,
completion notification and end-frame-to-next-wait distributions. These event
intervals are not isolated GPU timings, and their medians should not be summed
as though they came from one frame. Details are committed in
[vr-monado-measurements.json](vr-monado-measurements.json).

## Failures traced and changes made

1. **Vulkan ICD path.** The staged ICD referenced
   `/usr/lib64/libGLX_nvidia.so.0`, which does not exist inside the Ubuntu guest.
   The experiment creates `/opt/vr/vulkan.json` pointing at the mounted driver
   library. Vulkan device enumeration then succeeds. The common driver staging
   and existing desktops are not changed by this sandbox-local fix.
2. **Xvnc Vulkan presentation.** NVIDIA Vulkan enumeration without DISPLAY
   succeeds. With Xvnc, `vkGetPhysicalDeviceSurfacePresentModesKHR` returns an
   error. The tested game path uses its built-in OpenGL compatibility renderer
   and VirtualGL, while Monado renders to offscreen Vulkan images.
3. **Godot's OpenGL OpenXR binding.** The bundled Godot 4.3 passes a null
   `glxFBConfig` and zero visual ID. This is the known
   [Godot issue #116254](https://github.com/godotengine/godot/issues/116254), fixed
   upstream by [PR #116256](https://github.com/godotengine/godot/pull/116256).
   `OXR_LAB_ALLOW_MISSING_GLX_CONFIG=1` relaxes validation of those two fields
   only. Monado's GLX client uses the provided live context and drawable, and
   does not consume either unused field. Other validation stays enabled.
   This opt-in compatibility exception is not an upstream fix or a claim of
   specification compliance by the old game engine.
4. **Offscreen target callback.** GDB caught a null call from
   `renderer_submit_queue`: the upstream `debug_image` target omitted
   `is_shared_presentable_image`. The patch supplies the correct false return.
   It also exposes a target-name environment setting so this target can be
   selected explicitly instead of entering the deferred X11 target.
5. **Evidence capture.** An opt-in request file activates Monado's existing
   GPU readback implementation. It disables the direct-projection shortcut for
   that capture frame, ensuring the scratch eye image actually contains the
   composed frame. A request arriving later waits for the next composed frame.
   The helper writes an atomic PPM result. The patch also corrects a width-used-
   as-height typo in the readback extent. Continuous capture is not enabled.
6. **Virtual haptics.** The remote driver advertises a haptic output but has no
   implementation. The game repeatedly sends output/stop commands. The patch
   keeps returning `XRT_ERROR_NOT_IMPLEMENTED` and emits one warning per virtual
   controller instead of a log message for every call. Haptics are unsupported.

All source edits are in [monado-vr-lab.patch](monado-vr-lab.patch). The gVisor
engine did not need a new change for this experiment. The missing optional
Godot vendor extension, some game resource/UI warnings, and absent audio device
remain visible in the logs. The game used its dummy audio driver; audible audio
was not validated.

## Reproduce

Work in `~/scratch/general-vm`, with the existing lab prerequisites and an
allocated NVIDIA GPU. The preparation command downloads pinned dependencies
and runs apt/build commands **inside the selected sandbox**.

```bash
python scripts/env.py start vr-example --launch-options \
  --gpu 0 --guest-gs --memory-mib 24576 --cgroup v1 --no-runtime-debug
python scripts/prepare-vr-lab.py vr-example
python scripts/vr-lab.py vr-example start --hz 120 --mirror pbo
python scripts/vr-lab.py vr-example capture
python scripts/vr-lab.py vr-example play
python scripts/vr-lab.py vr-example capture
python scripts/vr-lab.py vr-example metrics --tail-seconds 30
```

Allow game initialization to finish before Play, and allow at least 30 seconds
of gameplay before collecting a 30-second gameplay window. `play` sends a real
virtual-controller trigger at the menu button, not a game-specific API call.
It assumes the initial headset/origin and menu placement from Open Saber 0.5.0.
It is not a general command to restart a level from any screen.

To compare without the continuous desktop mirror:

```bash
python scripts/vr-lab.py vr-example stop
python scripts/vr-lab.py vr-example start --hz 120 --mirror none
python scripts/vr-lab.py vr-example play
python scripts/vr-lab.py vr-example capture --output runs/vr/example-eye.png
```

`--mirror none` deliberately leaves the desktop window without current game
pixels. The OpenXR rendering path continues; `capture` reads its composed eye
image. `--mirror sync` and `--mirror pbo` retain continuous desktop viewing.
These are VirtualGL readback choices, not substitutes for the XR compositor.

For arbitrary virtual input, positions are meters and orientations are OpenXR
quaternions in x/y/z/w order:

```bash
python scripts/vr-lab.py vr-example input -- --device head --position 0.1 1.6 0
python scripts/vr-lab.py vr-example input -- --device left --position -0.3 1.2 -0.6
python scripts/vr-lab.py vr-example input -- --device right --click a
python scripts/vr-lab.py vr-example input -- --reset
```

The remote protocol is native-ABI-specific and unauthenticated inside the
sandbox. Port 4242 is not forwarded to the host. The adapter checks the protocol
magic and packet size; the C ABI probe verified packet/head/controller sizes
376/128/120 and the relevant offsets against the exact upstream header. The
protocol provides no per-action completion acknowledgement; visual acceptance
was used. This is not yet a production XR fast-I/O API.

`vr-lab.py stop` stops only the experiment's game and Monado services. The sandbox
and its Xvnc session remain running. The existing `env.py` lifecycle controls
remain available for the whole sandbox; graphics live snapshots remain unsupported.

## Pinned inputs, verification and evidence

- gVisor source: `59487a05f5e858d5b20a36d980036ccea2ad82ab`; existing immutable
  runtime `dae2b000bd86b4757abc79fc2da0d563910dd4ccc9afc2c4765e47c3b8447431`.
- [Monado source](https://gitlab.freedesktop.org/monado/monado/-/tree/f8dfadfeaeb46df3eec17bd76b7abdf42a79108c):
  `f8dfadfeaeb46df3eec17bd76b7abdf42a79108c`, plus the committed lab patch.
- [Monado metrics schema](https://gitlab.freedesktop.org/monado/utilities/metrics/-/tree/41e64fa19837534028c6db89ea641b4cc1552b3c):
  `41e64fa19837534028c6db89ea641b4cc1552b3c`.
- [Published Open Saber release](https://github.com/leandrodreamer/BeepSaber/releases/tag/v0.5.0):
  source tag `b8c8e97d3361c8a20c61cb38e92593266fead258`; Linux binary SHA-256
  `adc189b96d7321f9c7d142c9243c244394c3ad35a95b8a7fdf0ea4779b74e526`.
- [Monado requirements](https://monado.freedesktop.org/getting-started.html) and
  [VirtualGL readback options](https://github.com/VirtualGL/virtualgl/blob/d045fcf1777e7b47913ea363d14f71b1fb26b2b9/doc/index.html#L3903)
  provide the corresponding upstream architecture/documentation context.

The pinned preparation procedure was replayed successfully in the disposable
sandbox, including patch application and compilation. The launch, stop, virtual
Play input, eye capture and metrics commands were exercised live. Python syntax,
shell syntax, source-patch application and upstream C ABI/layout checks passed.
The main Gym Anything codebase and its tests were not changed.

Local artifacts under ignored `runs/vr/` include `monado-gdb.log`,
`reproduction-prepare.log`, `view-info.json`, `remote-abi.json`, binary timing
captures and reports. Inspected images include `xr-gameplay-first.png`,
`eye-menu-actual.png`, `eye-gameplay.png`, `hz120-gameplay-verified.png`, and
`pbo-gameplay-desktop.png`. They distinguish the desktop mirror from a runtime
eye image. Downloads and built binaries are deliberately ignored; authored
scripts, source patches and measurement summaries are committed.

The live experiment is `vr-monado-01`; its host VNC port is 41647. Consult
`env.py status` for current ports after any recreation. Wayland, physical headset
delivery, audio, haptics, robust XR action acknowledgements and broad VR-game
compatibility have not been validated by this first experiment.

## Continuous observation follow-up

The original one-shot input/capture interface above remains available.
[Continuous VR I/O](vr-continuous-io.md) adds a bounded host-memory image stream,
lossless background recording, viewing video, and an opt-in Monado state-received
acknowledgement for persistent input. Its measurements include observation costs;
the no-readback game FPS above is not model-observation throughput.

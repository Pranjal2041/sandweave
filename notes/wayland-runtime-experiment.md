# Wayland runtime experiment

Tested on 2026-09-08 in a separate `vr-wayland-01` sandbox on `babel-u5-28`,
Slurm allocation 10333558. The original VR/VNC session and both Resolve desktops
were preserved. No host sudo, KVM, host driver changes or gVisor source changes
were used.

**Working result:** headless GNOME Wayland with an NVIDIA compositor, Xwayland
using shared-memory presentation, Open Saber rendering on the L40S through
VirtualGL, Monado virtual controllers/headset, and synchronized stereo capture
plus background recording. The desktop game mirror was visually verified.

This is an experimental additional display configuration. Native NVIDIA Wayland
application presentation did not pass, and this experiment does not establish a
speedup over Xvnc. The existing fast-I/O desktop API remains Xvnc-only; Wayland
desktop screenshots below are diagnostic PNG captures, not a qualified fast-I/O
backend. Monado stereo I/O is independent of that desktop API.

## What runs and how it was verified

The probe uses Ubuntu 22.04, GNOME Shell 42.9, Mutter 42.9-0ubuntu9, Xwayland
22.1.1, driver 610.43.02 and the same allocated L40S as the earlier VR test.
It has four advertised guest CPUs, weighted sharing and a 24 GiB guest-page
budget. The gVisor revision remains
`59487a05f5e858d5b20a36d980036ccea2ad82ab`.

`engine-gpu` selects the matching NVIDIA EGL implementation. Both Weston 9's
headless GL renderer and GNOME's surfaceless renderer initialized without a
DRM render node inside the sandbox. GNOME initially logs that its surfaceless
renderer is "without GPU", referring to its device-discovery path. A diagnostic
`eglMakeCurrent` interposer queried the actual context:

```
renderer=NVIDIA L40S/PCIe/SSE2 version=OpenGL ES 3.2 NVIDIA 610.43.02
```

This establishes GPU composition, beyond merely observing NVIDIA libraries
in the process. The published Open Saber 0.5.0 binary still runs with its
original Godot 4.3 engine. Its X11 window is directed to the new Xwayland display
using that display's actual authority file. The desktop paths are:

```
Open Saber GPU rendering → VirtualGL readback → Xwayland SHM → GNOME GPU composition
Open Saber OpenXR submission → Monado GPU composition → paired-eye ring → host client
```

The second path retains the existing controller acknowledgement and lossless
recorder. Both eyes remain 960×1080, packed into a 1920×1080 observation. The
captured GNOME desktop is 1280×800; the game mirror window is 1280×720.

The final desktop screenshot is
`runs/wayland/gnome-shm-gameplay.png`. It was opened and inspected: GNOME's panel,
the Open Saber window, controllers and advancing level are visible. Paired game
images are in `runs/wayland/gnome-shm-pbo-1/`, with lossless frames and index in
its `frames/` directory. These are separate from the user's original VNC desktop.

## Comparison with continuous input and recording

Each row requested a 120 Hz Monado compositor, a maximum of 120 stereo pairs/s,
120 input updates/s, 20 seconds of warmup and 20 seconds of measurement. All
used the same fresh test sandbox and game configuration, with the PBO desktop
mirror enabled and four recording workers. Shared host/GPU load was uncontrolled.

| Configuration | Stereo pairs/s | Game submissions/s | Input updates/s | Saved / received |
|---|---:|---:|---:|---:|
| Xvnc, run 1 | 72.62 | 46.06 | 119.39 | 1453 / 1453 |
| Xvnc, run 2 | 73.20 | 45.59 | 119.29 | 1457 / 1457 |
| GNOME Wayland + Xwayland SHM | 84.05 | 40.87 | 119.20 | 1682 / 1682 |

All three had zero recording queue drops. The final Wayland consumer skipped one
ring sequence, separately from the recorder count. The second Xvnc run began
during cleanup of the preceding failed EGLStream desktop, so it is supporting
context rather than a fully quiescent control. The first Xvnc run followed a
completed compositor stop.

The Wayland run delivered more compositor observations but fewer application
submissions. Its compositor reuse fraction was 63.9%, versus 58.4% in the first
Xvnc run. New observation sequences can contain reused application frames; they
must not be reported as unique game-state FPS. These short samples do not prove
that either display architecture is faster. The tested Wayland route still
performs VirtualGL readback and Xwayland-to-compositor transfer.

Latency in milliseconds, median / p95 / p99:

| Configuration | Host input → Monado ACK | Readback start → owned host stereo pixels |
|---|---:|---:|
| Xvnc, run 1 | 0.81 / 4.54 / 6.87 | 4.96 / 11.08 / 163.98 |
| Xvnc, run 2 | 0.84 / 4.50 / 6.30 | 4.88 / 10.04 / 172.09 |
| GNOME Wayland + Xwayland SHM | 1.02 / 4.47 / 7.82 | 5.07 / 9.98 / 139.01 |

Long-tail stalls remain: the Wayland run's worst input ACK was 115.65 ms and
worst readback-to-client sample 267.98 ms. Monado's ACK confirms state installation,
not application processing, rendered effects or physical headset display.

For the user's original `vr-monado-01` environment, the preceding stereo test
measured **0.61 ms median, 2.17 ms p95, 5.81 ms p99** input ACK latency and
118.66 updates/s, with a 195.41 ms maximum. Those remain historical measurements
of that environment; they were not replaced with timings from this fresh sandbox.
[Original stereo results](vr-stereo-io.md).

## Failures that constrain the result

1. **Default NVIDIA Wayland client discovery.** `glmark2-wayland` failed at
   `eglGetDisplay` on both GPU compositors. NVIDIA egl-wayland 1.1.21 requires a
   DRM identity from `wl_drm` or DMA-BUF feedback; the sandbox compositor has none.
   Its source rejects this before selecting the internal EGL device.
   [Pinned NVIDIA source](https://github.com/NVIDIA/egl-wayland/blob/f81fcd016bb232c12ba59dddb240648203caaf4c/src/wayland-egldisplay.c#L844).
   Selecting Ubuntu's older 1.1.9 platform library explicitly also failed; that
   separate failure was recorded but not fully isolated.

2. **Xwayland's automatic EGLStream path.** The game rendered stereo images,
   but its desktop window was invisible. GNOME reported a missing Cogl external
   EGL-image feature, and Xwayland repeatedly failed to create its stream surface.
   Mutter 42 enables that feature only in its EGL-device renderer mode, excluding
   the surfaceless mode used here. Switching to GLES does not change that gate.
   [Mutter's feature gate](https://github.com/GNOME/mutter/blob/42.9/src/backends/native/meta-renderer-native.c#L1035).
   `XWAYLAND_NO_GLAMOR=1` gives the working SHM presentation path while preserving
   VirtualGL GPU rendering and GNOME GPU composition. Earlier EGLStream timing
   runs are retained as failures, not accepted desktop benchmarks.

3. **Published game's native Wayland OpenGL mode.** It failed NVIDIA EGL setup,
   fell back to Mesa llvmpipe and produced shader/framebuffer errors. It was
   terminated after ten seconds and does not qualify as GPU VR. Independently,
   Godot 4.3 explicitly rejects OpenXR's OpenGL binding for Wayland.
   [Godot 4.3 source](https://github.com/godotengine/godot/blob/4.3/modules/openxr/extensions/platform/openxr_opengl_extension.cpp#L146).

4. **Native Wayland Vulkan mode.** Monado created the Vulkan instance and the
   game discovered the L40S, but no device passed its graphics-plus-presentation
   check. Godot then aborted on an empty device list. Native Vulkan presentation
   is unresolved; an instance or GPU enumeration alone is insufficient evidence.

5. **Headless GNOME startup and cleanup.** D-Bus screenshot/input services became
   ready after the Xwayland process existed. The launcher now waits for those
   services, allows up to 60 seconds and dismisses the initial overview through
   a real virtual keyboard event. Stopping the private session can leave child
   services waiting; systemd uses a five-second termination grace period followed
   by cleanup of that owned unit. This does not stop the sandbox or other desktops.

The [machine-readable report](wayland-runtime-measurements.json) retains success
and failure summaries, native probe commands and representative diagnostics.
Full logs, recordings and generated screenshots remain under `runs/wayland/`.

## Reproduction

Start a separate GPU sandbox using the normal VR preparation instructions, then
install the diagnostic packages **inside that sandbox**: `mutter`, `weston`,
`mesa-utils`, `strace`, `libnvidia-egl-wayland1` and `glmark2-wayland`. The base
image already contains GNOME Shell, Xwayland and Python D-Bus. The renderer
interposer requires the compiler installed by `prepare-vr-lab.py`.

```bash
python scripts/wayland-lab.py vr-wayland-example start --probe-renderer
python scripts/wayland-lab.py vr-wayland-example status
```

Use `x11_display` and `xauthority` from that result; authority filenames change
when the compositor restarts:

```python
from vr_stream import VRStream

with VRStream('vr-wayland-example', x11_display=state['x11_display'],
              xauthority=state['xauthority'], mirror='pbo') as vr:
    pair = vr.latest()
    left, right = pair.left, pair.right
    ack = vr.input({'right': {'trigger_value': 0.5}})
```

The existing measurement CLI accepts `--x11-display` and `--xauthority` as well.
Its default still selects Xvnc `:1`. The stream owns the game and Monado; the
Wayland helper owns its separate compositor session. Close the stream before
stopping that session:

```bash
python scripts/wayland-lab.py vr-wayland-example capture --output runs/wayland/desktop.png
python scripts/wayland-lab.py vr-wayland-example stop
```

`--xwayland-renderer auto` reproduces the unqualified EGLStream/GBM experiment;
the default is the visually verified `shm` route. `--probe-renderer` is optional
diagnostic instrumentation, not required for rendering.

Validation: VR-stream, fast-I/O and environment suites passed (34 tests total),
Python compilation passed, and live start/status/capture/stop plus game/input/
stereo recording were exercised. This is not a complete Wayland desktop action
audit, lifecycle/snapshot qualification or native-buffer performance result.

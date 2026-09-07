# Resolve playback performance investigation

Measured 2026-09-07 on the same L40S and allocation as
[Resolve acceptance](resolve-gpu.md). The original desktop was preserved;
application comparisons used disposable native and gVisor sessions.

## What the measurements establish

- During 30 seconds of approximately 9 fps playback, host NVIDIA device index 3
  reported 0–2% SM utilization, 0% memory-controller utilization, and 80–82 W.
  Another job reserved 40,866 MiB of VRAM, but the GPU was not compute-saturated.
- The CPU sharing controller recorded **zero pauses** for the original guest
  and the four-CPU diagnostic guest. Its quota controller is not imposing this
  playback limit.
- Native Resolve reached **24 fps**, including after restricting all of its
  existing threads to host CPUs 0–3. It used the same application version, GPU,
  staged NVIDIA libraries, VirtualGL 3.1.5 EGL backend, 1920x1080 VNC display,
  DNxHR source and copied project/grade. This is an affinity comparison, not
  identical CPU discovery: native Resolve initially detected 64 logical CPUs.
- A fresh four-CPU gVisor guest reproduced **8.9 fps**. VirtualGL profiling
  measured approximately **0.9–1.0 ms/frame readback** and **1.4–1.6 ms/frame
  blitting** during playback. Its total frame delivery was approximately
  10 fps. These per-stage service times are not a complete additive frame
  latency breakdown; they do show spare capacity in those two stages.
- A fresh guest with **16 advertised CPUs and execution slots** still reported
  **8.9 fps**, with zero CPU-broker pauses. Increasing this setting from four
  to sixteen did not fix the slowdown. Its log confirms detection of 16 CPUs.
- Source-viewer playback without the timeline grade was approximately 9.9 fps
  for the 720p DNxHR clip. The previously exported 1080p ProRes clip played at
  approximately 6.9 fps. Removing this grade or changing to that codec did not
  achieve real-time playback.
- Stopping the diagnostic guest's PipeWire PulseAudio service/socket during
  playback did not improve 8.9 fps. This is not equivalent to a clean launch
  with a different audio backend, so it does not fully exclude audio handling.

The immediate explanation is **not supported as another job consuming GPU
compute**, a CPU-broker quota, or slow VirtualGL readback/blitting. Native
playback demonstrates that this GPU and graphics bridge can sustain this clip's
24 fps. The exact remaining wait in the guest playback pipeline is unresolved;
do not call it a fundamental gVisor limitation or a proven engine bug yet.

Native control used Metacity without compositing and did not have a working
audio server. The full guests used GNOME and PipeWire. Consequently the native
comparison alone does not isolate the engine from every desktop configuration
difference.

## Transfers independently of Resolve

[`gpu-transfer-probe.py`](../scripts/gpu-transfer-probe.py) measures 8 and 32 MiB
host-to-device and device-to-host copies, with pageable and pinned memory.
Each case warms up three times and measures ten synchronized copies. Before
device-to-host copying it clears the host buffer, then checks the returned data.
Native and the 16-CPU guest both passed. Pinned 32 MiB transfers were around
1.3 ms in both, roughly 24 GiB/s. Pageable results are recorded separately;
this does not establish that every Resolve-specific CUDA operation is fast.

Run it through `engine-gpu python3` in a guest, or through
`scripts/gvisor-guest-gpu.sh python3` in the native Apptainer control, with the
same selected NVIDIA devices and staged GPU directory. No KVM is used.

## Diagnostic limits and reproduction details

The native control helper now accepts repeatable `--bind` arguments for a private
application installation and dependency directory. A copy of the extracted
official installer payload supplied `/opt/resolve`; missing OpenCL-loader and
XCB libraries were staged from the working guest. The private project copy was
made using SQLite backups. Its media paths were relocated for the native
container; the original project was not rewritten. Native command metadata is
in ignored `runs/native-resolve-perf6.json`.

Set `VGL_PROFILE=1` **before launching Resolve**. Resolve redirects stderr into
`~/.local/share/DaVinciResolve/logs/ResolveDebug.txt`, which therefore contains
VirtualGL's `Readback`, `Blit` and `Total` lines. See the
[VirtualGL profiling documentation](https://github.com/VirtualGL/virtualgl/blob/main/doc/perfmeasurement.txt)
for the distinction between stage throughput and total delivered frame rate.

A five-second main-thread syscall trace spent approximately 3.47 seconds in
2,324 short `pselect6` calls. That establishes waiting, not the cause of the
wait. A later all-thread trace substantially perturbed scheduling and must not
be used as an uninstrumented frame-time measurement. `runsc debug --profile-cpu`
returned `unknown method` because these guests were not launched with `--profile`.
CPU profile collection would require a separately configured diagnostic launch.

Selected evidence is committed under `gpu-evidence/resolve-perf-*` and
`gpu-evidence/gpu-transfers-*`. Large traces, installer copies, private project
copies and transient launch logs remain ignored under `runs/` or node-local
scratch. No performance fix to the runtime or Resolve wrapper is claimed here.
All diagnostic desktops were stopped afterward. The original `resolve-gpu2`
desktop remains running with playback paused and loop playback disabled.

Validation for the diagnostic helpers: native and guest CUDA transfer probes
passed their data checks; the native launch helper's extra binds were exercised
by the working Resolve control; Python compilation, shell syntax and Git diff
checks passed. The engine source was not changed in this investigation.

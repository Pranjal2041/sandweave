# Resolve playback: repaired GPU completion notifications

Measured 2026-09-07 on babel-u5-28, allocation 10333558, NVIDIA L40S device
minor 0 / UUID `GPU-203c2df2-d777-ba3e-e218-dfc79347f5a5`, driver 610.43.02.
Engine commit **b832209** repairs playback from **8.9 fps to 24 fps** for the
same five-second DNxHR project and desaturation grade. The fixed guest uses
four advertised CPUs, 48 GiB, full GNOME, PipeWire audio and 1920x1080 VNC.
Resolve and NVIDIA binaries are unchanged. No KVM, host sudo or host driver
configuration changes are involved.

## Cause and repair

The non-directfs device path opens NVIDIA devices through the device gofer.
Upstream [`lisafs.fdTracker.DonateFD`](https://github.com/google/gvisor/blob/0a1316b0d180600212bd607aa0ccfe2a9b09a899/pkg/lisafs/communicator.go)
sets `O_NONBLOCK` on donated descriptors. That is appropriate for many proxied
files, but the NVIDIA driver's
[`nvidia_poll`](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/610.43.02/kernel-open/nvidia/nv.c#L2171)
calls `poll_wait()` only when `O_NONBLOCK` is **clear**. With the flag set,
an immediate readiness check can still succeed, but no kernel wait-queue
registration is established for later GPU events.

Consequently, nvproxy's host epoll adapter cannot wake the guest when the GPU
finishes. CUDA's event handler eventually notices completion at its 100-ms
polling deadline. Resolve queues two host callbacks per frame, and its next
work waits on completion. Fast kernel execution, GPU memory copies and GL
readback therefore coexist with roughly ten delivered frames per second.

The repair clears the donation-added `O_NONBLOCK` in nvproxy before registering
the host FD with fdnotifier. It makes the gofer path match nvproxy's existing
direct-open path, which already strips the flag. The shared helper also repairs
the device-reopen path. Failed flag repair closes the descriptor and propagates
the error. Other gofer files retain their existing behavior. No polling interval
is shortened, no application callback is replaced, and no isolation boundary
is removed. This is an integration bug in the upstream code paths used by this
configuration, rather than an inherent cost of executing without KVM.

## Causal measurements

[`gpu-callback-probe.py`](../scripts/gpu-callback-probe.py) submits an asynchronous
32-MiB CUDA memset, queues a host callback, then waits on a Python event rather
than calling CUDA synchronization while waiting. It tests both
`cuStreamAddCallback` and `cuLaunchHostFunc`, checks callback delivery/status and
the final device data. Five warmups precede 40 measured samples per API.
A 20-ms idle interval lets CUDA's event thread enter its wait before submission.

| Median callback latency | Old guest | Fixed guest | Native Apptainer |
|---|---:|---:|---:|
| `cuStreamAddCallback` | 80.332 ms | 0.220 ms | 0.167 ms |
| `cuLaunchHostFunc` | 80.283 ms | 0.261 ms | 0.124 ms |

The old guest's approximately 80-ms delay follows the remaining part of its
100-ms poll after the 20-ms idle interval. Without deliberate idling, the old
guest alternates between fast callbacks and approximately 100-ms stalls.
An initial empty-stream test with `cuStreamSynchronize` did not expose the
problem: it could finish while the driver was actively progressing work.
Tracing every poll also changed the race timing and hid the long stalls.

The fixed full desktop repeatedly displayed **24 fps**. VirtualGL reported
23.97–24.01 delivered frames/s in the saved steady playback excerpt. A native
control using the same delayed CUPTI tracer also displayed 24 fps. The original
guest was preserved, and the separate old-runtime diagnostic reproduced 8.9 fps.
Host `/proc` descriptor inspection confirms that the original NVIDIA character
device FDs retain `O_NONBLOCK`, while those in the fixed guest do not.

CUDA API tracing showed that Resolve's enqueue calls were fast: the old guest
issued 100 callbacks and 650 kernel launches per five seconds, versus 240
callbacks and 1,560 launches natively. These are enqueue timings, not callback
completion timings; that distinction motivated the asynchronous reproducer.

The independent CUDA/OpenGL buffer handoff probe also passed. Mapping/unmapping
were about 5–6 microseconds and synchronized 256-byte GL readback about 0.16 ms
in native and old guest runs. The fixed guest passes it too. The existing CUDA
transfer probe passes its data checks on the new runtime. Removing GNOME
compositing did not fix playback; a clean launch with audio disconnected still
fell short of real time. The final successful desktop retains both GNOME and
working audio.

## Reproduce and inspect

Build/publish with `python scripts/stage-gvisor.py`, then start a fresh GPU guest
using the [normal Resolve setup](resolve-gpu.md#reproduce). Existing guests keep
their original immutable runtime; updating the staged executable does not
upgrade a running guest. The fixed runtime is
`tools/runtime-builds/79623840e3b5cab0482c5f7881ec9a1765608ce1a65ac8da48ab0181304a1aea`.

Run `engine-gpu python3 /opt/engine-gpu/probes/gpu-callback-probe.py` inside it.
The optional `--iterations` and `--idle-ms` parameters control sampling. Run
`engine-gpu-gl -nodl python3 /opt/engine-gpu/probes/gpu-glx-interop-probe.py --benchmark`
as the GUI user with DISPLAY and XAUTHORITY for the independent handoff check.

The launcher now accepts optional `--profile` to expose gVisor CPU/block profiling
RPCs for diagnostics; it is off by default and not inherited from snapshots.
Example collection after launching that diagnostic guest:

```bash
scripts/gvisor-host.sh --gpu 0 /lab/tools/gvisor-socket/runsc \
  --root=/local/gvisor/state debug --profile-cpu=/lab/runs/playback.pprof \
  --duration=10s DIAGNOSTIC_GUEST
```

[`gpu-api-profile.c`](../scripts/gpu-api-profile.c) is an optional CUPTI tracer.
It times driver APIs including pointers obtained via `cuGetProcAddress`. Its
five-second counters are approximate interval summaries across concurrent
threads; overlapping API times must not be added as frame latency. It does not
trace GPU activity or require GPU performance counters.

The working build used the staged CUDA headers and the host-installed Nsight
Systems 2026.1.3 CUPTI library, copied into the ignored GPU tools directory:

```bash
cp /opt/nvidia/nsight-systems/2026.1.3/target-linux-x64/libcupti.so.13.3 tools/gpu/compat/
ln -sf libcupti.so.13.3 tools/gpu/compat/libcupti.so.13
gcc -Wall -Wextra -Werror -O2 -shared -fPIC \
  -I tools/gpu/python/nvidia/cuda_cupti/include \
  -I tools/gpu/python/triton/backends/nvidia/include scripts/gpu-api-profile.c \
  -L tools/gpu/compat -l:libcupti.so.13 -Wl,-rpath,/opt/engine-gpu/compat \
  -pthread -o tools/gpu/compat/libgpuapiprofile13.so
chmod a+rX tools/gpu/compat/libcupti.so.13.3 tools/gpu/compat/libgpuapiprofile13.so
```

Before launching Resolve, set `GPU_API_PRELOAD=1`, `GPU_API_DELAY=45`, and
`VGL_PRELOAD=/opt/engine-gpu/compat/libgpuapiprofile13.so`. Logging appears in
ResolveDebug.txt. `CUDA_INJECTION64_PATH` caused Resolve's processing engine
initialization to fail both natively and in the guest, including with CUPTI 12.8
and 13.3; use the validated delayed preload instead. Normal launches need none
of these profiling settings.

## Validation, evidence and current desktop

The complete nvproxy unit-test target passes, including descriptor flag
preservation, continued descriptor usability and invalid-descriptor handling.
The six existing lab GPU allocation/identity tests pass. Runtime publication,
Python compilation, shell syntax, strict C compilation and the final profiler's
live callback smoke test passed. GPU save/restore remains unqualified and refused
by the launcher; repairing the reopen code does not change that status.

Evidence is under `notes/gpu-evidence/`: `gpu-callback-{old,fixed,native}.json`,
`gpu-device-blocking-flags.json`, `gpu-interop-latency-*.jsonl`,
`gpu-transfers-fixed.json`, `resolve-playback-fixed.{png,txt}`,
`resolve-api-{before,native}.txt`, `nvproxy-blocking-tests.txt` and
`gpu-api-profiler-smoke.txt`. Large traces and private project copies stay ignored.
The engine recovery bundle is `checkpoints/gpu-notifications-b832209/`.

**resolve-optfix** is the fixed desktop: node VNC 35869, Mac
`127.0.0.1:5913`, password `labvnc01`, forward ID `e271ab7b`. The original
**resolve-gpu2** remains on Mac port 5912 with its old engine and saved export.
Temporary native controls and **resolve-opt1** have been stopped.
This qualifies the tested project, not arbitrary codecs or heavier timelines.

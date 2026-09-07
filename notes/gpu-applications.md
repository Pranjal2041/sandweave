# GPU applications: Earth and the Resolve prerequisites

Tested 2026-09-07 in the standalone lab on `babel-u5-28`, Slurm 10333558.
No main-project changes, KVM, host sudo, or new gVisor engine patches were used.
The engine remains `8c8b1437b27ce0fb61a9db17c5feb33242b7bde3`.

## Accepted behavior and remaining work

Google Earth Pro 7.3.7.1327 renders through NVIDIA L40S, accepts typed searches,
flies to search results, and displays detailed Golden Gate Bridge imagery.
The new GNOME application entry was selected through VNC and launched the same
accelerated application. Earth's About dialog identifies NVIDIA Corporation,
and its GLX trace identifies `NVIDIA L40S/PCIe/SSE2`.

Exiting after navigation repeatedly produced SIGSEGV in Earth's
`earth::geobase::LoadObserver` destruction during `GShutdown`. A native Apptainer
control with the same `myplaces.kml` and Google settings reproduced the exact
function/offset stack. Disabling VirtualGL's dlopen interposer with `-nodl` did
not prevent the gVisor crash. A native run with fresh settings had closed without
this crash. These observations rule out gVisor as a necessary cause; they do not
yet distinguish an Earth data/lifecycle issue from the shared graphics setup.
Reopening through the application menu succeeded, and searching worked again.
This is a recorded stability issue, not a fully qualified Earth lifecycle.

DaVinci Resolve has **not been installed or run**. The official free Linux
21.0.4 download requires registration. Searches of the user-authorized Mac
Downloads directory and Gmail accounts found no installer or previous Blackmagic
registration. The official form is open on the Mac; contact details or completion
by the user are required. No invented contact information was submitted.

CUDA/OpenGL sharing, a useful prerequisite for an editor combining compute and
graphics, passes in both native Apptainer and gVisor. This does not establish
Resolve compatibility, playback performance, codec support, or export behavior.

## Earth failures and fixes

### Default X visual was never mapped to a rendering configuration

The initial gVisor run reported “Could not access Graphics Card”. A matched native
Apptainer run with the same Earth image, NVIDIA libraries, VirtualGL 3.1.5 and
Xvnc reproduced the failure. `glXChooseVisual` could choose visual 0x395, but
Earth's Qt window path used the root visual 0x21. VirtualGL returned no framebuffer
configuration and a null context for that root visual in both runtimes.

VirtualGL's `server/glxvisual.cpp` assigns default framebuffer attributes in the
order returned by `XGetVisualInfo`. With this Xvnc visual list, the root visual
appears too late to receive an assignment. The
[small shim](../scripts/gpu-visual-order.c) moves it to the front for a
screen-wide query. It preserves the count, every visual, and the relative order
of the remaining entries; it does not hardcode 0x21 or alter the application's
requested visual. With the shim, both runtimes map the root visual to a valid
configuration and create a real NVIDIA context.

A [VirtualGL maintainer discussion](https://groups.google.com/g/virtualgl-users/c/sTjocHkTKHI)
describes the same ordering problem with another X server and application, and
discusses giving the default visual precedence. It is supporting context, not an
upstream report of this exact Earth failure. Source inspection used VirtualGL
commit `d045fcf`; the tested binary remains the pinned official 3.1.5 package.

### Keyboard input needed an unstaged VirtualGL dependency

After rendering worked, typing caused a crash when VirtualGL attempted to load
`libxcb-keysyms.so.1`. The lab had extracted the VirtualGL package without that
runtime dependency. Staging Ubuntu Jammy `libxcb-keysyms1_0.4.0-1build3_amd64.deb`
and adding its library directory to `engine-gpu` fixed input. Both native and
gVisor then handled typed searches. This dependency is pinned in `stage-gpu.sh`:

```
1d62f96a793cc1aa0df860de3f7d83edea2fa461d97e2b3d4d7f7e7a10e55f42
```

The staging script also builds the shim with host GCC and X11 development
headers. This node's glibc 2.34 build runs with the guest's glibc 2.35. The shared
library is replaced atomically so staging cannot truncate a mapped binary.
Downloaded packages and compiled libraries remain ignored and reproducible;
the shim source and launch scripts are committed.

## Reproduce the accelerated desktop

```bash
cd ~/scratch/general-vm
bash scripts/stage-gpu.sh 0
python scripts/run-gvisor.py --detach --gpu 0 --guest-gs \
  --memory-mib 24576 --cgroup v1 --no-runtime-debug my-gpu-apps -- /sbin/init
```

After GNOME is ready, use **Activities → Google Earth Pro (GPU)**. The GPU
entrypoint installs that menu entry on fresh launches; the fixture builder
includes `engine-gpu-gl` automatically. To launch from a terminal in the guest:

```bash
engine-gpu-gl /usr/bin/google-earth-pro
```

The wrapper adds the root-visual shim, then invokes `engine-gpu vglrun -d egl0`.
VirtualGL remains local to the launched application, not globally preloaded.

The kept environment is **gpu-apps1**: four advertised CPUs, a 24 GiB guest-page
budget, the usual 1 GiB runtime guard, weighted sharing of the inherited Slurm
CPU pool, and the internet network policy. GPU minor 0 is host NVML index 3,
UUID `GPU-203c2df2-d777-ba3e-e218-dfc79347f5a5`. VNC is node loopback port
**41711**. A `ut` forward was created on the Mac at **127.0.0.1:5911**. The lab
VNC password is `labvnc01`. Ports and this live environment last only as long as
the allocation and processes do. The original four user desktops are preserved.

For a native comparison, use the same staged resources and selected device:

```bash
python scripts/run-native-gpu-app.py native-earth -- \
  env LD_PRELOAD=/opt/engine-gpu/compat/libvisualorder.so \
  VGL_TRACE=1 vglrun -d egl0 /usr/bin/google-earth-pro
```

This helper uses the lab's existing `native-root` Apptainer filesystem, a private
home/config/tmp, Xvnc on display :97, and a random loopback VNC port. It writes
the process-group leader, port and session path to `runs/native-earth.json`.
Run one native graphics control at a time because the display number is fixed.
Omit the `LD_PRELOAD` assignment to reproduce the initial context failure.
These native controls are comparison workloads, not the isolated guest engine.

## CUDA/OpenGL interop

The [probe](../scripts/gpu-glx-interop-probe.py) creates a GLX context on the root
visual, checks NVIDIA identity and a pixel readback, then registers a 256-byte
OpenGL buffer with CUDA. CUDA maps it, fills every byte with 42 and unmaps it;
OpenGL reads it back and verifies the bytes. Resources are released on success.

With VirtualGL's default `libdlfaker` interposer, both native and gVisor fail at
`cuGraphicsGLRegisterBuffer` with CUDA error 999. Passing **`-nodl`** disables
that interposer and both pass. Keep it as an application-specific option: this
does not qualify every app that dynamically opens graphics libraries.

```bash
scripts/gvisor-host.sh --gpu 0 /lab/tools/gvisor-socket/runsc \
  --root=/local/gvisor/state exec my-gpu-apps runuser -u ga -- \
  env DISPLAY=:1 XAUTHORITY=/home/ga/.Xauthority \
  /usr/local/bin/engine-gpu-gl -nodl \
  python3 /opt/engine-gpu/probes/gpu-glx-interop-probe.py
```

This is a correctness check, not a throughput benchmark. Single-GPU access,
driver qualification, VRAM/time accounting limits and deferred GPU snapshots
remain as documented in [single-gpu.md](single-gpu.md).

## Evidence and checks

- [Initial native failure](gpu-evidence/earth-native-failure.png) and
  [before/after GLX traces in both runtimes](gpu-evidence/earth-visual-traces.json).
- [Earth's NVIDIA About dialog](gpu-evidence/earth-nvidia-about.png),
  [detailed bridge imagery](gpu-evidence/earth-nvidia-bridge.png), and
  [the selected GPU menu entry](gpu-evidence/earth-gpu-launcher.png).
- [CUDA/OpenGL positive and negative controls](gpu-evidence/cuda-opengl-interop.json).
- [Fresh guest fixtures and ordinary-user driver check](gpu-evidence/gpu-app-fixtures.txt).
- [Observed Earth shutdown crash](gpu-evidence/earth-exit-crash.txt) and
  [matching native shutdown stack](gpu-evidence/earth-native-exit-crash.txt).

The full updated GPU staging script passed. Six GPU allocation/identity tests
passed. Python/shell syntax and the shim's `-Wall -Wextra -Werror` build were
checked. Live acceptance included mouse navigation, keyboard search, graphics
identity, menu launch, and real CUDA/OpenGL data sharing. Raw logs and disposable
session state remain ignored under `runs/` and node-local storage.

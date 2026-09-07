# Single-GPU access without KVM

Validated 2026-09-07 on `babel-u5-28`, in existing Slurm job 10333558. This changes
only the standalone lab. The gVisor engine remains at
`8c8b1437b27ce0fb61a9db17c5feb33242b7bde3`; no additional kernel/engine patch was
needed for the successful GPU workloads.

## Results

| Workload | Observed result |
|---|---|
| PyTorch 2.7.1 + CUDA 12.8 | Single L40S, real backpropagation and AdamW updates; 120-step smoke test and 1,600-step comparison passed. |
| Training correctness | 21,504,128-parameter MLP, BF16 autocast, synthetic fixed input/target batch. Finite gradients, changed weights, decreasing loss. Every sampled loss matched native. Model and optimizer file save/load passed. |
| Training speed | 304.39 steps/s in gVisor; 306.88 steps/s in native Apptainer. Ratio 99.19%. |
| Firefox 155 | Interactive WebGL cube rendered using NVIDIA; VNC reversal control worked. `about:support` reports WebRender, NVIDIA L40S/PCIe/SSE2, and VirtualGL EGL. No Firefox sandbox-disabling flag or forced acceleration preference was used. |
| EGL/GLX probe | NVIDIA OpenGL 4.6 context through VirtualGL 3.1.5's EGL backend, presented to existing Xvnc. |
| Device boundary | Only `/dev/nvidia0`, `nvidiactl`, `nvidia-uvm` visible. Guest-root creation of `/dev/nvidia1` succeeded, but opening it failed with ENOENT. `/dev/kvm` absent. Non-GPU launch exposes no NVIDIA devices. |
| Google Earth Pro 7.3.7 | NVIDIA rendering, typed search, detailed imagery and menu launch passed after VirtualGL setup fixes. A shutdown crash also reproduces in native Apptainer with the same saved places. See [application evidence](gpu-applications.md). |
| CUDA/OpenGL interop | CUDA wrote a mapped OpenGL buffer; OpenGL readback verified all 256 bytes. Native and gVisor pass with VirtualGL `-nodl`; both fail without it. |
| Resolve | CUDA/OpenGL, timeline playback, color adjustment, project reopening and ProRes export passed; see [Resolve acceptance and limits](resolve-gpu.md). |
| Games / Vulkan window presentation | Not tested yet. |
| GPU snapshots | Persistent-filesystem cold restore is tested. Small CUDA live restore passed experimentally; graphics live restore failed. See [current snapshot results](gpu-filesystem-snapshots.md). |

[Machine-readable training evidence](gpu-evidence/training-comparison.json),
[device test](gpu-evidence/device-boundary.json),
[OpenGL information](gpu-evidence/opengl-info.txt),
[Firefox graphics report](gpu-evidence/firefox-support.png),
[interactive Firefox check](gpu-evidence/firefox-webgl.png), and
[Earth failure](gpu-evidence/earth-failure.png) are committed with this note.

The training comparison measures 1,590 steps after ten warmup steps: 5.224 seconds
versus 5.181 seconds. Whole probe time, including imports, CUDA initialization,
checks, and model/optimizer serialization, was 14.943 versus 10.769 seconds.
Both used Python 3.10, the same staged PyTorch/CUDA libraries, four PyTorch CPU
threads, the same GPU, and the inherited Slurm CPU pool. The gVisor guest had four
advertised CPUs, a 16 GiB page budget and its usual weighted CPU policy. Verbose
engine logging was disabled. Peak framework GPU allocation was 655,463,424 bytes.
This is one matched pair on a small, compute-heavy synthetic workload, not a
general training benchmark or a claim that all training pipelines lose only 0.8%.
Data loading, larger models, custom kernels and long runs remain unqualified.

## How it works

Apptainer individually binds the selected GPU device and its two control devices.
The OCI spec includes the same three devices. gVisor's `nvproxy` handles their
driver operations, while systrap executes the application's CPU instructions.
The host NVIDIA kernel driver executes real work on the GPU. No KVM, host sudo,
driver installation, administrator change or NVIDIA runtime hook is involved.

`stage-gpu.sh` copies matching host NVIDIA user-space libraries, extracts pinned
VirtualGL 3.1.5 and installs the pinned Python packages. The guest receives these
as a read-only `/opt/engine-gpu` mount. Before executing the requested guest
entrypoint, `engine-gpu-init` registers the matching driver libraries first in
the guest linker cache and links its `nvidia-smi` into `/usr/local/bin`.
Ordinary guest commands can therefore load CUDA/NVML without a wrapper.
`engine-gpu` additionally sets library/ICD/Python paths for a command.
For graphics, `vglrun -d egl0` renders offscreen with NVIDIA EGL
and transfers the frames to the existing guest Xvnc display. The application,
Firefox content processes, desktop and Xvnc all remain inside gVisor.

The exact host driver is 610.43.02. This ABI exists in our gVisor source but is
classified as unqualified, so GPU launches explicitly use
`--nvproxy-allow-unsupported-driver`. They do not pretend to use another driver
version. Compute, utility, graphics and video capabilities are enabled.

### Device numbering on this node

`--gpu N` selects device minor N, meaning `/dev/nvidiaN`. That number is **not**
necessarily the displayed `nvidia-smi` index. Our selected device is:

- Device: `/dev/nvidia0`
- UUID: `GPU-203c2df2-d777-ba3e-e218-dfc79347f5a5`
- PCI address: `0000:61:00.0`
- Host `nvidia-smi` index: **3**

The active Slurm allocation includes numeric GPU IDs 0–3, and its visible NVML
devices map to minors 0–3, in a different order. The launcher checks numeric
Slurm membership, then verifies the device's UUID is visible through host NVML
and records the identity. `SLURM_STEP_GPUS`, when present, narrows the job list.
This allocation's mapping is established; other clusters' GRES numbering and
partial allocations need equivalent qualification. CUDA inside the single-device
guest sees logical device zero. `NVIDIA_VISIBLE_DEVICES` is descriptive; the
device mounts and OCI device set establish the actual exposed device set.

## Reproduce

Stage dependencies before starting a GPU environment:

```bash
cd ~/scratch/general-vm
bash scripts/stage-gpu.sh 0
python scripts/run-gvisor.py --detach --gpu 0 --guest-gs \
  --memory-mib 16384 --cgroup v1 --no-runtime-debug my-gpu -- /sbin/init
```

The dependency list is [pinned](gpu-python-requirements.txt). Downloaded binaries,
wheels and extracted packages remain ignored under `tools/gpu` and `downloads`.
The VirtualGL package SHA256 is
`df3f7788ce41b182a47c0d298e5cd6d2d63579522cb41825970b7726e825485e`.
The driver copy records its version and original files in
`tools/gpu/driver/driver.json`; it must match the loaded host driver at launch.

After boot, run training:

```bash
scripts/gvisor-host.sh --gpu 0 /lab/tools/gvisor-socket/runsc \
  --root=/local/gvisor/state exec my-gpu /usr/local/bin/engine-gpu \
  python3 /opt/engine-gpu/probes/gpu-training-probe.py --steps 1600
```

To start Firefox after GNOME is ready, create a profile owned by `ga`, then run
this **inside the guest**, retaining the GPU wrapper:

```bash
install -d -o ga -g ga /home/ga/gpu-firefox
runuser -u ga -- env DISPLAY=:1 XAUTHORITY=/home/ga/.Xauthority \
  XDG_RUNTIME_DIR=/run/user/1000 \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus MOZ_ENABLE_WAYLAND=0 \
  /usr/local/bin/engine-gpu vglrun -d egl0 /opt/firefox/firefox \
  --no-remote --profile /home/ga/gpu-firefox \
  file:///opt/engine-gpu/probes/gpu-webgl.html
```

The original GPU test desktop was `gpu-ready1`, VNC port **54815**, password `labvnc01`:

```bash
ssh -N -L 5910:127.0.0.1:54815 pranjala@babel-u5-28
```

That desktop and the original `moodle-user1`, `earth-user1`, and `ready-clone2`
desktops were subsequently stopped at the user's request. The current live
desktop and connection are documented in [Resolve acceptance](resolve-gpu.md).

The native training control used the existing `native-root` Apptainer filesystem,
bound the same three devices and same `/opt/engine-gpu` directory, selected CUDA
device zero, and ran this same probe under Python 3.10. Its private `/tmp` was
bound to node-local `gpu-native-tmp` so model serialization did not use the full
home filesystem. Raw run logs remain under `runs/gpu-training-*.jsonl`.

### Ordinary-terminal NVIDIA driver resolution

On 2026-09-07 at 06:04:12 guest time, `ga` ran `apt install nvidia-utils-390`.
The transaction also installed `libnvidia-compute-390`, both version
390.157-0ubuntu0.22.04.2. Plain `nvidia-smi` then loaded the new guest NVML
390.157 and failed with `Driver/library version mismatch` against host driver
610.43.02. Those old libraries came from this apt transaction, not the base
image. Accelerated Firefox continued to use the matching staged driver through
`engine-gpu`.

GPU launches now put `/opt/engine-gpu/driver/lib` in
`/etc/ld.so.conf.d/00-engine-nvidia.conf`, rebuild the cache, and expose the
matching binary at `/usr/local/bin/nvidia-smi`. This configuration was also
applied to `gpu-ready1` without restarting its desktop or Firefox. The installed
390 packages remain in the guest; no host packages or driver were changed.
An existing shell can run `hash -r` to forget the old executable path.
VirtualGL remains an application launch choice; this does not globally preload
it or claim acceleration for the desktop compositor.

[Driver-resolution evidence](gpu-evidence/driver-resolution.json) records tests
as ordinary UID1000, with no `LD_LIBRARY_PATH` or `LD_PRELOAD`: plain
`nvidia-smi`, successful NVML/CUDA initialization, one CUDA device, and loaded
library paths pointing to driver 610.43.02. Both the existing desktop (after
another plain `ldconfig`, simulating the cache rebuild from a package install)
and a fresh disposable GPU launch passed. The old `/usr/bin/nvidia-smi -L`
also succeeded after the cache fix. Reproduce the ordinary-user probe after
staging dependencies:

```bash
scripts/gvisor-host.sh --gpu 0 /lab/tools/gvisor-socket/runsc \
  --root=/local/gvisor/state exec my-gpu runuser -u ga -- \
  env -u LD_LIBRARY_PATH -u LD_PRELOAD \
  python3 /opt/engine-gpu/probes/gpu-driver-probe.py
```

## Outstanding work and limits

Earth's earlier context-creation failure was reproduced in native Apptainer.
A small Xlib visual-order shim fixes VirtualGL's mapping of the default visual;
staging its missing XCB key-symbol dependency also fixes keyboard input. The new
`engine-gpu-gl` wrapper and “Google Earth Pro (GPU)” menu entry use this setup.
[Application notes](gpu-applications.md) include reproduction commands, native
comparisons, screenshots, and the separate shutdown crash. `gpu-ready1` retains
working Firefox; `gpu-apps1` holds the new Earth desktop.

GPU VRAM and GPU time do not fall under the existing guest page limit or CPU
weights. The training probe voluntarily caps its own PyTorch allocator to 25%
of VRAM; that is not a runtime-enforced GPU quota. Other workloads in the same
Slurm allocation can use the same GPU. Existing network/CPU/page-memory controls
remain present; exposing the GPU additionally exposes the permitted host driver
interface. Nested Docker GPU setup has not been qualified in this experiment.

The PyTorch model/optimizer file roundtrip is ordinary application persistence.
It is not a whole-environment snapshot or preservation of live GPU contexts.
CPU-only whole-environment snapshots retain their existing behavior.

Checks: six GPU allocation/identity unit tests, fourteen snapshot-store tests,
Python and shell syntax checks; successful fresh GPU and non-GPU launches;
guest-root device-node negative test; early GPU snapshot refusal; live training,
and actual VNC interaction plus Firefox's graphics report.

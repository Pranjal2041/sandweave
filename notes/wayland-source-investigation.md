# Wayland source investigation

Research date: 2026-09-07. Scope: add display and device capabilities to the
existing no-KVM, no-host-sudo Slurm architecture. Xvnc remains an available
backend; Wayland is an additional backend. Monado/XR remains in scope for VR.
This is not a selection of one exclusive display technology.

This investigation read local source, public upstream source, commit history,
issues and documentation. It did not start environments, open GPU devices,
change drivers, build software or run benchmarks. Only instructions and research
documentation changed. Earlier host-device observations are explicitly reused
from [the display audit](display-architecture-research.md), not remeasured.

## Source revisions and evidence limits

- Local gVisor: `59487a05f5e858d5b20a36d980036ccea2ad82ab`, clean at inspection.
- Mutter upstream main: `02b4d88ac3bcfc0f7b8d8937808a77b6f0d285a6`.
- NVIDIA egl-wayland2: `6530db705112724af9a6eb9981d6f6a4a2f37564`.
- Existing recorded GPU: L40S, driver 610.43.02; accessible render node,
  inaccessible physical card nodes; PRIME and timeline capability queries passed.

Upstream Mutter is not the guest's installed Mutter. This review does not
establish a minimum usable distribution release, the guest's installed protocol
versions, or working GPU presentation. GitLab merge-request pages did not load
through the browser fetcher; the changes and their associated MR identifiers
were recovered from GNOME's GitHub mirror commit metadata and source.

## Headless access has explicit upstream support

Mutter's `init_gpus()` selects render-node devices in headless mode.
`create_render_device()` opens them without taking display control.
`meta_backend_native_create_launcher()` bypasses the session launcher, and
initialization sets `META_KMS_FLAG_NO_MODE_SETTING`. These are actual code paths,
not assumptions derived from the word "headless".
[Native backend source](https://github.com/GNOME/mutter/blob/02b4d88ac3bcfc0f7b8d8937808a77b6f0d285a6/src/backends/native/meta-backend-native.c).

Two upstream changes explain why an older compositor can fail despite adequate
render-node access:

| Change | Concrete effect |
|---|---|
| [6bd2fd6a, MR 3805](https://github.com/GNOME/mutter/commit/6bd2fd6a743be8f4d3ff33fc801c55fa1df1a576), authored June 2024 | Discover render nodes directly through udev; avoid first opening a card node that the user cannot access. |
| [6c567e67, MR 4448](https://github.com/GNOME/mutter/commit/6c567e67474c502f51773632231e212c893a0de9), authored January 2025 | Headless sessions no longer create the launcher that associates the compositor with a logged-in display session. |

This establishes a compositor architecture compatible with render-only access.
It does not establish that gVisor currently implements its required device ABI.

The current GPU renderer calls `gbm_create_device`, creates an
`EGL_PLATFORM_GBM_KHR` display, and allocates/imports buffers with format and
modifier information.
[GBM renderer source](https://github.com/GNOME/mutter/blob/02b4d88ac3bcfc0f7b8d8937808a77b6f0d285a6/src/backends/native/meta-render-device-gbm.c).
The no-GPU fallback uses a different surfaceless renderer; reaching a desktop
through that fallback would not qualify GPU gaming.
[Renderer selection](https://github.com/GNOME/mutter/blob/02b4d88ac3bcfc0f7b8d8937808a77b6f0d285a6/src/backends/native/meta-renderer-native.c).

The historical EGLDevice/EGLStream page-flip path was removed by
[2842e9be, MR 5079](https://github.com/GNOME/mutter/commit/2842e9beaa5111a6accebdac794273b02c29052a),
authored May 2026. The earlier implementation also opened a device before its
EGLStream fallback. Our working VirtualGL EGLDevice rendering therefore does
not establish a drop-in way to start this compositor without its device path.

## NVIDIA client presentation and synchronization

NVIDIA's `egl-wayland2` uses DMA-BUFs and a driver interface available from the
560 series. Its documented compositor requirements include linux-dmabuf support;
linux-drm-syncobj explicit synchronization is needed for full functionality.
The project describes reduced performance and out-of-order frames without
explicit synchronization. Its frame-throttling behavior also depends on
presentation-time, FIFO and commit-timing protocols.
[NVIDIA requirements](https://github.com/NVIDIA/egl-wayland2/tree/6530db705112724af9a6eb9981d6f6a4a2f37564).

The timeline implementation makes these concrete libdrm calls:

- `SyncobjCreate` and `SyncobjDestroy`.
- `SyncobjHandleToFD`, followed by sending the descriptor to the compositor.
- `SyncobjTransfer` between timeline points and temporary synchronization objects.
- `SyncobjExportSyncFile` and `SyncobjImportSyncFile` for completion fences.

[Timeline source](https://github.com/NVIDIA/egl-wayland2/blob/6530db705112724af9a6eb9981d6f6a4a2f37564/src/wayland/wayland-timeline.c).
This is a source-derived operation list for that client path, not a complete
trace of all compositor, EGL, Vulkan or Xwayland operations.

Device discovery also matters. The client prefers opening a render node, but
uses DRM/PCI information and can inspect both primary and render-node identities
to match an EGL device. A guest device pathname alone is insufficient: device
numbers, sysfs/udev identity and the GPU selected by nvproxy must agree. Providing
metadata for a card node does not imply giving the guest physical display
control.
[Device-discovery source](https://github.com/NVIDIA/egl-wayland2/blob/6530db705112724af9a6eb9981d6f6a4a2f37564/src/wayland/wayland-display.c).

## Concrete local integration gaps

| Layer | Source finding and implication |
|---|---|
| GPU device registration | `scripts/gvisor_gpu.py:allocated_device()` supplies only the selected `/dev/nvidiaN`, `/dev/nvidiactl` and `/dev/nvidia-uvm`. It does not register a DRM render node. |
| Device implementation | The inspected engine has no DRM render-node driver or DRM ioctl table. Merely adding a device to the launch specification does not implement its behavior. |
| Generic passed descriptors | `pkg/sentry/fsimpl/host/host.go:Ioctl()` only forwards `FIONREAD`; `ConfigureMMap()` accepts regular files. `--pass-fd` is not a generic DRM passthrough mechanism. |
| GPU buffer descriptors | PRIME export returns host descriptors; imports contain guest descriptors. A driver implementation must translate them, preserve lifetime and support required mapping/polling operations. |
| Synchronization | Syncobj handles, timeline descriptors and sync-file fences need correct import/export and completion handling. Buffer allocation alone would leave frame ordering and reuse unresolved. |
| Discovery | The allocated GPU needs consistent guest-visible DRM and PCI metadata for Mutter, libdrm and NVIDIA's client libraries. The engine already has a separate RDMA sysfs implementation, but not the equivalent DRM topology. |
| Driver packaging | `stage_driver()` copies matching `libnvidia-*` libraries, but explicitly stages only GLVND EGL-vendor and Vulkan ICD JSON files. It does not stage the EGL external-platform JSON registrations for GBM/Wayland. Existing guest registrations might supply these, but that has not been verified. |
| Fast-I/O selection | `scripts/fast_io.py` currently rejects backends other than Xvnc. Wayland input/capture and lifecycle attachment are not implemented. |

The packaging distinction is visible in NVIDIA's own discovery code, which
includes GBM and both Wayland external-platform registrations and their libraries.
[NVIDIA container-toolkit graphics discovery](https://github.com/NVIDIA/nvidia-container-toolkit/blob/main/internal/discover/graphics.go).

The engine already has useful, narrower FD mechanisms:

- `nvproxy/version.go` registers NVIDIA resource-manager object export/import
  operations; `frontend.go:ctrlHasFrontendFD()` translates existing NVIDIA
  frontend descriptors around the host ioctl.
- `frontendExportToDMABufFD()` wraps CUDA GPUDirect RDMA exports as guest FDs.
  Its wrapper exposes the host FD to rdmaproxy and closes it on release; it is
  not a complete graphics DMA-BUF implementation.

These prevent the overstatement that "gVisor cannot share GPU objects." They
also do not establish compatibility with a DRM compositor. NVIDIA's
[DRM ABI header](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/main/kernel-open/nvidia-drm/nv_drm_common_ioctl.h)
contains separate memory-import/export, allocation, mapping and fence operations.
Which subset driver 610.43.02 actually exercises remains unmeasured.

## Fast I/O is available at the compositor interface

Mutter's ScreenCast API exposes monitor/window capture and virtual monitors,
with PipeWire stream identifiers. Current source allows virtual-monitor modes
to specify size and refresh rate. These interfaces do not require a VNC stream.
They are explicitly private APIs without cross-version compatibility promises.
[ScreenCast contract](https://github.com/GNOME/mutter/blob/02b4d88ac3bcfc0f7b8d8937808a77b6f0d285a6/data/dbus-interfaces/org.gnome.Mutter.ScreenCast.xml).

RemoteDesktop exposes keyboard, pointer and touchscreen operations and an EIS
connection. This gives input access to native Wayland surfaces as well as the
desktop containing Xwayland applications. It does not expose a gamepad or XR
headset interface.
[RemoteDesktop contract](https://github.com/GNOME/mutter/blob/02b4d88ac3bcfc0f7b8d8937808a77b6f0d285a6/data/dbus-interfaces/org.gnome.Mutter.RemoteDesktop.xml).

There are still separate measurement questions: event accepted by the input
backend, application processing, application frame submission, compositor frame
availability, and delivery to the caller. An immediate cached capture is not
evidence of an immediate application response. A GPU-native compositor may
avoid intermediate application readbacks, but CPU RGB observations still need
a final readback. No speedup or sub-10-ms latency is claimed here.

## Issues checked and what they actually establish

- [gVisor 11695](https://github.com/google/gvisor/issues/11695) remains open:
  host/sandbox Unix-socket FD sharing. This is not evidence that FD passing
  between applications inside the same Sentry is unavailable.
- [gVisor 11650](https://github.com/google/gvisor/issues/11650) remains open:
  Firefox/Waypipe `sendmsg()` receives EINVAL. Maintainer comments identify
  `MSG_CMSG_CLOEXEC` on a send call, and question why a receive-only flag is
  present. The inspected engine's send flag mask still rejects it. It is a
  specific compatibility concern, not evidence that all Wayland clients fail;
  the report itself describes successful Chromium use.
- The scoped GitHub issue/PR search for DRM, Wayland and GBM found no ready DRM
  render-node proxy integration. This is a bounded search result, not proof
  that no implementation exists anywhere.

## Controller and VR scope

Gamepad support needs its own guest device interface regardless of desktop
backend. Monado integrates at the XR runtime boundary: headset/controller poses,
eye images and frame timing. It is not an alternative name for the Wayland
desktop. Monado requires Vulkan external-memory and semaphore FD capabilities
for client/compositor texture submission.
[Monado requirements](https://monado.freedesktop.org/getting-started.html).

The existing NVIDIA object-FD handlers are relevant building blocks for that
investigation; they do not establish that Monado's external-memory and
semaphore combinations work. An offscreen XR route does not automatically
depend on completing the same physical-display or GBM path as Mutter.

Native GPU X11 also supports 3D applications; Wayland is not a universal
requirement for 3D or XR. The limitation being investigated is the lab's current
Xvnc/VirtualGL presentation architecture. Monado documents both X.Org and
Wayland direct-display integrations.
[Monado display integration](https://monado.freedesktop.org/direct-mode.html).

## Current result

The source investigation identifies an upstream headless compositor path that
fits the recorded render-node permissions, plus concrete missing device,
metadata, buffer, synchronization and packaging integration in this lab.
It establishes where implementation work belongs. Starting the compositor,
presenting real OpenGL/Vulkan/Xwayland applications, validating fast I/O,
measuring frame times, and exercising lifecycle operations remain untested.
The Xvnc runtime is unchanged; no Wayland or XR support is advertised as working.

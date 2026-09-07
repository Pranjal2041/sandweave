# Display architecture after removing KVM

Follow-up: the user chose to implement and qualify Xvnc first. That work is now
recorded in [Xvnc fast I/O](xvnc-fast-io.md). The recommendation below is retained
as research history; Mutter/Wayland implementation remains deferred.

Research dated 2026-09-07. Existing desktops were preserved. This investigation
read local and upstream source, documentation and issue discussions, and made
one read-only capability probe on the allocated GPU's render node. No desktop,
compositor, rendering benchmark, driver installation or engine build was started.

Recommendation: retain Xvnc as the compatibility baseline while investigating
headless GNOME/Mutter with Xwayland as the principal alternative. Qualify that
architecture before investing in a custom Xvnc fast-I/O extension. There is no
measurement yet establishing which display backend is faster in this lab.

**Why Xvnc is currently useful**

Xvnc is an X server with a virtual framebuffer and a VNC server integrated into
it. It supplies a complete display without a physical monitor, virtual terminal
or ownership of physical display hardware. Its virtual framebuffer and X input
interfaces fit the existing GNOME desktop and X11 applications. See
[TigerVNC's Xvnc documentation](https://tigervnc.org/doc/Xvnc.html).

This lab's [working GPU route](single-gpu.md) renders applications through
VirtualGL's NVIDIA EGL backend and transfers their output into Xvnc. That uses
the `/dev/nvidia*` interfaces already implemented by nvproxy. It avoided needing
a DRM/GBM compositor path before those interfaces had been qualified.

Xvnc is not a requirement imposed by KVM or by gVisor. Changing the isolation
engine does not choose a display server. It also matters that the VNC network
protocol and the Xvnc framebuffer are separate: local screenshot capture can
read the framebuffer without encoding, transmitting or decoding VNC. Human
viewing may still use VNC independently. Replacing VNC transport alone is not
the same as removing VirtualGL readback or changing desktop composition.

**The alternative with a substantial architectural difference**

A headless GPU compositor can keep application images on the GPU while composing
the desktop. A final CPU screenshot then reads back the composed output. For
remote video, a GPU buffer may instead feed a hardware encoder. This could
avoid VirtualGL's separate application readbacks and some CPU composition work;
it does not remove the final readback needed for an ordinary RGB/PIL observation.

| Current route | Candidate route |
|---|---|
| Application GPU rendering through VirtualGL | Application GPU rendering through native window-system drivers |
| Application output copied into the Xvnc desktop | Application GPU buffers shared with the compositor |
| Desktop composed into CPU-accessible pixels | Desktop composed on the GPU |
| Shared-memory screenshot of final desktop | Final capture into CPU memory, or GPU buffer for video |

The gain depends on workload and capture frequency. A continuously GPU-composed
desktop can be more expensive than a mostly idle CPU framebuffer for some
software-only tasks. New buffers, synchronization and renderer scheduling can
also introduce waits. No latency number follows just from choosing Wayland.

**Why GNOME/Mutter is the principal candidate**

Mutter already implements a headless native backend. The examined code avoids
taking normal display-device control in headless mode, avoids the seat launcher,
and sets the no-modesetting flag. See its
[native backend](https://github.com/GNOME/mutter/blob/main/src/backends/native/meta-backend-native.c).
The latest commit affecting that file at inspection was
`fb9aabcbf35754077536078938160bcdc3d6f5e9`.

It also provides the relevant mechanisms directly:

- Virtual monitors and streams through `RecordVirtual`, `RecordMonitor` and
  `PipeWireStreamAdded` in its
  [ScreenCast interface](https://github.com/GNOME/mutter/blob/main/data/dbus-interfaces/org.gnome.Mutter.ScreenCast.xml).
- Pointer/keyboard injection and, in current versions, `ConnectToEIS` in its
  [RemoteDesktop interface](https://github.com/GNOME/mutter/blob/main/data/dbus-interfaces/org.gnome.Mutter.RemoteDesktop.xml).
- PipeWire supports shared memfd and DMA-BUF media buffers; ordinary RGB
  capture need not involve video encoding. See the
  [PipeWire library architecture](https://docs.pipewire.org/page_library.html).

This would retain GNOME as the desktop, with Xwayland serving existing X11
applications. NVIDIA documents accelerated OpenGL/Vulkan Xwayland through its
GBM/GLAMOR path; see [NVIDIA's GBM documentation](https://download.nvidia.com/XFree86/Linux-x86_64/550.142/README/gbm.html).
The document also specifies host DRM KMS and user-space library requirements.
Those requirements must be checked against driver 610.43.02 on this node;
the older document is architectural evidence, not version-specific validation.

Native Firefox, existing Earth/Resolve X11 applications, CUDA/OpenGL interop,
menus, dialogs, clipboard, resolution changes and existing setup scripts all
need testing. Xwayland compatibility does not prove that X11 automation tools
can control GNOME's native Wayland surfaces. Input and whole-desktop capture
must go through the compositor-level interface where necessary. API availability
must also be checked in the actual guest's GNOME/PipeWire versions; upstream
main is not the installed environment.

A D-Bus reply or libei frame is not automatically a processed-input or repaint
acknowledgment. [libei's sender API](https://libinput.pages.freedesktop.org/libei/api/group__libei-sender.html)
groups input events into frames, but our delivery contract still needs explicit
verification. A capture stream can also be paced by the compositor. Measure
request latency, source-frame age and action-to-visible-effect separately.

**What this cluster actually allows**

The selected GPU at PCI `0000:61:00.0` has render node `/dev/dri/renderD128`.
Its device permissions were `0666`, while physical `/dev/dri/card*` nodes were
not accessible to this user. More decisively, UID 2710891 successfully opened
the selected render node and queried capabilities with `libdrm.drmGetCap`:

| Capability | Returned value |
|---|---:|
| PRIME import/export bitmask | 3 |
| Synchronization objects | 1 |
| Timeline synchronization objects | 1 |

[Raw recorded result](display-evidence/render-node-capabilities.json).
The descriptor was closed immediately. The probe allocated no GPU buffers,
submitted no rendering and requested no modesetting. These are advertised
capabilities, not a successful buffer-sharing/compositor test; PRIME capability
bits are unconditional in recent Linux kernels. The `nvidia_drm.modeset` sysfs
parameter could not be read by this user; no permission change was attempted.

Render nodes are explicitly designed for GPU use without DRM-master ownership;
see the kernel's [render-node interface](https://docs.kernel.org/gpu/drm-uapi.html#render-nodes).
Thus lack of host sudo does not by itself rule out this route. The node's
existing configuration still has to support the actual compositor workload.

**The concrete gVisor gap**

The audited engine is `59487a05f5e858d5b20a36d980036ccea2ad82ab`. Its NVIDIA proxy
supports the device route we already use, but there is no DRM render-node
driver implementation in the inspected device code. Upstream also documents
its restricted GPU device set in [GPU support](https://gvisor.dev/docs/user_guide/gpu/).

Passing a render descriptor through `--pass-fd` is insufficient: the generic
host file's `Ioctl` supports `FIONREAD` and otherwise falls back to the default,
and its `ConfigureMMap` accepts regular files rather than arbitrary character
devices. A DRM-aware proxy needs to handle the relevant ioctls, memory mappings,
returned descriptors and synchronization objects.

There is some DMA-BUF code in nvproxy, but it implements CUDA GPUDirect RDMA
export and exposes the resulting host descriptor to rdmaproxy. It is not a
general graphics PRIME/GBM implementation. NVIDIA's maintainer explicitly
distinguishes resource-manager DMA-BUF export from graphics buffer sharing
through nvidia-drm in this [driver discussion](https://github.com/NVIDIA/open-gpu-kernel-modules/discussions/243).

The useful engine experiment is therefore to identify and implement the render
node operations used by the selected compositor and clients. Likely categories
include device/capability queries, GEM objects, PRIME descriptor import/export,
NVIDIA memory-sharing ioctls, mappings, and synchronization. The actual list
must come from successful native traces and failing guest traces, not guesses.
The [NVIDIA DRM implementation](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/main/kernel-open/nvidia-drm/nvidia-drm-drv.c)
marks many relevant ioctls as allowed on render nodes. Physical modesetting
ownership is a separate concern from proxying render-node operations.

Keeping compositor, applications and PipeWire inside each environment preserves
the existing process/filesystem boundary. Moving the compositor outside would
require cross-boundary graphics-buffer transport and a separate lifecycle
design. gVisor's [host FD-sharing request](https://github.com/google/gvisor/issues/11695)
and [Firefox/Waypipe report](https://github.com/google/gvisor/issues/11650) show
why host display forwarding is not automatically transparent. Both were open
at inspection; neither establishes failure of an all-in-guest compositor.

**Other candidates considered**

| Candidate | Assessment for this lab |
|---|---|
| Xvnc with direct local fast I/O | Strong existing compatibility baseline. CPU-accessible framebuffer is favorable for RGB screenshots. |
| Xvfb or Xorg with a dummy driver | Can remove bundled VNC service, but remains a CPU framebuffer architecture; no demonstrated intrinsic capture advantage. Human viewing would need another service. |
| Newer Xvnc with DRI3 | Worth qualification, but does not bypass the gVisor DRM gap. NVIDIA acceleration claims need real validation. |
| Headless GNOME/Mutter + Xwayland | Principal alternative: preserves GNOME and provides compositor-native monitor, capture and input mechanisms. Requires the device/buffer path to work. |
| Weston headless / wlroots compositor | Useful focused renderer and protocol diagnostics. A replacement compositor changes the desktop and cannot silently replace GNOME in task environments. |
| NVIDIA Xorg with no attached display | Can preserve X11 compatibility, but NVIDIA display-driver, console and device-control requirements are not established inside this engine. It is not unlocked merely by setting a headless Xorg option. |
| TurboVNC, KasmVNC, Xpra | May improve viewing, encoding or integration, but changing remoting software does not alone remove GPU readback or prove faster local RGB capture. |

The [TigerVNC DRI3/NVIDIA issue](https://github.com/TigerVNC/tigervnc/issues/1773)
is still open. Its original hardware-acceleration claim was corrected after
testing; choosing a vendor string was not sufficient evidence. This does not
prove driver 610 cannot work, but it rules out treating a documented render-node
flag as a completed compatibility result.

[Weston's current documentation](https://wayland.pages.freedesktop.org/weston/toc/running-weston.html)
provides headless operation with GL, Vulkan and Pixman renderers. The examined
headless GL backend requests `EGL_PLATFORM_SURFACELESS_MESA`; that is not the
`EGL_EXT_platform_device` path tested by our VirtualGL setup. A renderer starting
successfully also does not prove that GPU buffers from independent applications
can be imported and composed. [KasmVNC's release history](https://github.com/kasmtech/KasmVNC/releases)
includes streaming/encoder work, which addresses a different cost from local
uncompressed observations.

**Decision gates before choosing a backend**

1. Establish headless GNOME/Mutter plus Xwayland on the same allocated GPU in
   an isolated native Apptainer control. Keep the existing desktops running.
   Verify actual GPU rendering and import/export, not just reported renderer
   names. Record versions and only the selected GPU's required device accesses.
2. Repeat with the same libraries and applications in a disposable gVisor
   environment. Identify the first missing DRM/buffer/synchronization operation.
   This distinguishes an engine implementation gap from host configuration or
   application incompatibility before investing in either display backend.
3. Compare the completed GPU desktop against Xvnc at equal resolution, CPU
   policy and workload. Measure application frame delivery, compositor cost,
   GPU/CPU transfers, fresh capture, cached capture and acknowledged input.
   Include CPU-only operation; the winning backend may differ by environment.
4. Test the existing Earth, Firefox, Resolve/CUDA and Moodle workflows, including
   their setup scripts and desktop interactions. Preserve the delivery and
   screenshot-freshness contract described in [fast-I/O research](fast-io-research.md).
5. Validate resident pause/resume, CPU live snapshots and GPU filesystem
   snapshots, with reconnect and display reconstruction. A different compositor
   does not solve the currently unsupported GPU graphics-state snapshot.

No default display backend is changed by this note. The source and host evidence
justify testing headless GPU composition before selecting an Xvnc-specific
optimization, while retaining the complete working environment as the baseline.

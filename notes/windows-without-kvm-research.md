# Windows inside the no-KVM sandbox: feasibility research

Research date: **2026-09-08**. The user requires Windows **here**, with fast
execution, and explicitly excludes TCG. The lab's no-KVM/no-host-sudo objective
remains in force. A slow emulated boot would not satisfy this task.

This is source/documentation research plus a read-only device check,
**not a Windows boot or graphics acceptance result**. The accepted Linux game
order remains Clone IT, FlightGear, Locomancer; Windows investigation takes
precedence for now. No Windows image or emulator was downloaded or launched.

**No deployable fast full-Windows solution has been established under all
these constraints.** There is strong architectural precedent for moving
Windows services into userspace while executing application instructions
directly: Microsoft's Drawbridge. The missing piece is a usable Windows
runtime with that adaptation, together with modern graphics and XR support.
This is not evidence that such an architecture is impossible.

## Direct execution through a Windows library OS

Drawbridge's Windows runtime included NTUM, an adapted NT kernel running in
user mode, and an adapted Win32 subsystem. Its platform interface exposed
threads, virtual memory and I/O streams through 45 downcalls. Microsoft
demonstrated unmodified Office 2010, Internet Explorer and IIS applications.
This is a concrete precedent for the user's proposed extra layer.
[Microsoft's Drawbridge description](https://www.microsoft.com/en-us/research/project/drawbridge/overview/).

The research paper describes substantial refactoring of Windows 7, including
GUI initialization and OS-service assumptions. One host implementation needed
no changes to the Windows host kernel. That proves neither an unmodified
Windows ISO nor the same runtime working on Linux/gVisor, and it is not a
modern VR performance result.
[Drawbridge paper](https://www.microsoft.com/en-us/research/wp-content/uploads/2016/02/asplos2011-drawbridge.pdf).

SQLPAL subsequently combined parts of Drawbridge with SQL Server's own
platform layer to run SQL Server on Linux. Microsoft removed library-OS
facilities that SQL Server did not need. This is practical evidence for the
approach on Linux, but SQLPAL is not a general Windows desktop runtime.
[SQL Server engineering account](https://www.microsoft.com/en-us/sql-server/blog/2016/12/16/sql-server-on-linux-how-introduction/).

The corresponding architectural direction for this lab would be:

```text
Windows application instructions execute directly on the x86-64 CPU
  adapted Windows NT / Win32 services in user space
    small platform layer for memory, threads, files and I/O
      gVisor's Linux interface and existing isolation
```

This is a design direction, not an available local stack. The research did not
locate a supported general Windows NTUM distribution/source package that we
can install here. An ordinary Windows kernel binary is not already converted
to this interface. Our existing Wine route executes Windows application code
directly with compatibility services, but it does not boot Microsoft's kernel;
it cannot silently stand in for a requirement to run the actual Windows OS.

## What “recreating KVM” can and cannot mean

Our Linux applications call the Sentry's Linux interface. The Sentry uses a
restricted set of host operations to implement it. This is not unrestricted
access to the host kernel or to privileged CPU execution.
[gVisor architecture](https://gvisor.dev/docs/architecture_guide/intro/),
[platform guide](https://gvisor.dev/docs/architecture_guide/platforms/).

Relevant local source, inspected at engine commit
`c2f78eb0077f8394917b9bcdece2e521e3150d76`:

- `pkg/sentry/platform/systrap/systrap.go:15–40` describes native user execution
  with syscall/signal interception. `subprocess.go:288` also enables syscall
  user dispatch when supported, with seccomp as a fallback. Neither mechanism
  provides a virtual ring 0, guest page-table hardware or VMX/SVM.
- `scripts/gvisor-host.sh:21` asserts `/dev/kvm` is absent in the containing
  Apptainer environment. Selecting gVisor's separate KVM *platform* would
  require actual host KVM access; it is not an emulated KVM service for guests.
- `pkg/sentry/loader/elf.go:123–126` accepts only ELF64.
- `pkg/sentry/devices/nvproxy/nvproxy.go:15` identifies a proxy for NVIDIA's
  **Linux** kernel-driver interface, not a Windows WDDM driver.

KVM's userspace API starts with opening `/dev/kvm`, then creating VM and vCPU
descriptors with ioctls. Reproducing those interface names does not grant
hardware virtualization access. A proxy to real host KVM could use that
acceleration, but would change this lab's no-KVM requirement.
[Linux KVM API](https://www.kernel.org/doc/html/latest/virt/kvm/api.html).

A read-only `srun` check on allocation `10361186`, node `babel-o9-20`, found
host kernel `5.14.0-687.25.1.el9_8.x86_64` and the following device paths:

| Host path | Exists | User read/write access checks |
|---|---|---|
| `/dev/kvm` | Yes | Yes / yes |
| `/dev/mshv` | No | No / no |
| `/dev/udmabuf` | Yes | No / no |
| `/dev/dri/renderD128` | Yes | Yes / yes |

These were `os.path.exists` and `os.access` checks, not device opens or
successful ioctls. The host's KVM path is deliberately hidden by our launcher;
we must distinguish that design constraint from a claim that this node has
no KVM device. No KVM use or host configuration change was attempted.

## New 2026 graphics work: Helios

The public `winboat-org/helios` repository was created on **2026-06-08**;
the companion `qemus/qemu-helios` packaging repository on **2026-08-23**,
according to their GitHub API metadata. These are repository creation dates,
not claimed stable-release dates. Revisions inspected:

| Source | Revision |
|---|---|
| [Helios](https://github.com/winboat-org/helios) | `6b4ef16fa566b7827f1a85d8994fed81ade11464` (September 7) |
| [QEMU Helios packaging/patches](https://github.com/qemus/qemu-helios) | `09c36b5c436d1fefbccfa349e452c59f4ad38dbb` (September 7) |

Helios supplies a Windows WDDM render/display adapter. Its graphics transport
uses Mesa Venus, with Direct3D translation through its DXVK/vkd3d integration.
The architecture is approximately:

```text
Windows Direct3D / Vulkan application
  Helios Windows drivers + Vulkan translation / Venus
    virtual virtio-gpu device
      QEMU + virglrenderer on Linux
        Linux Vulkan driver -> physical GPU
```

The GPU stays owned by Linux. The custom QEMU display changes handle Windows
desktop surfaces, including Vulkan image layouts and readback for VNC.
This is potentially relevant to both our GPU-sharing objective and Xvnc-based
desktop testing. QEMU's own VNC server and the outer Xvnc desktop are separate
presentation endpoints.
[Publisher's architecture](https://github.com/qemus/qemu-helios/blob/09c36b5c436d1fefbccfa349e452c59f4ad38dbb/readme.md).

The developers' roadmap dates the first complete desktop rendering to July 5,
2026, and records September D3D11/D3D12/Vulkan benchmark runs with unresolved
correctness and variability issues. These are upstream reports on their
hardware-accelerated VM setup, not measurements of our proposed runtime.
The repository explicitly says it is under heavy development and not ready
for production.
[Roadmap](https://github.com/winboat-org/helios/blob/6b4ef16fa566b7827f1a85d8994fed81ade11464/ROADMAP.md),
[maturity statement](https://github.com/winboat-org/helios/blob/6b4ef16fa566b7827f1a85d8994fed81ade11464/README.md).

There are concrete obstacles to transplanting it here:

- Its host guide expects KVM, `/dev/udmabuf`, suitable Vulkan support and a
  patched QEMU build. Its actual launcher selects `kvm,honor-guest-pat=on` and
  includes privileged host setup. We cannot run that launcher unchanged.
  [Host guide](https://github.com/winboat-org/helios/blob/6b4ef16fa566b7827f1a85d8994fed81ade11464/HOST.md),
  [launcher](https://github.com/winboat-org/helios/blob/6b4ef16fa566b7827f1a85d8994fed81ade11464/tools/launch-helios-gtk.sh).
- Our device staging currently exposes selected NVIDIA character devices,
  without a DRM render-node driver or a general graphics DMA-BUF proxy in
  gVisor. Merely adding `/dev/dri` or `/dev/udmabuf` to a device list does not
  implement their ioctls, mappings and returned descriptors.
  [Local graphics-interface audit](wayland-source-investigation.md).
- Venus has assumptions about exportable/mappable GPU memory and cache
  coherence. Its documented normal path maps that memory into a KVM guest.
  A new execution backend would have to establish equivalent memory behavior;
  the KVM configuration cannot simply be removed and presumed equivalent.
  [Mesa memory requirements](https://docs.mesa3d.org/drivers/venus.html#vk-memory-property-host-visible-bit).

NVIDIA is not categorically excluded: current Mesa documentation lists the
proprietary driver from 570.86 onward among tested Venus drivers, with host
configuration qualifications. Helios also keeps an RTX PRO 6000 Blackwell
Vulkan profile. Those observations do not validate the driver through nvproxy.
Some older warnings in Helios's host guide are broader than current Mesa
documentation; use the specific memory/synchronization requirements rather
than concluding NVIDIA cannot work.
[Mesa supported drivers](https://docs.mesa3d.org/drivers/venus.html#requirements),
[Helios host profile](https://github.com/winboat-org/helios/blob/6b4ef16fa566b7827f1a85d8994fed81ade11464/docs/reference/host-vulkan-profile-rtx-pro-6000-blackwell.json).

**Inference:** CPU execution and GPU command forwarding are separate
mechanisms. Helios may inform the graphics part of a future Windows runtime;
it does not supply the missing fast CPU execution backend. A copy-based graphics
transport could be another prototype direction if zero-copy mappings are the
blocker, but it would require explicit resource/synchronization semantics and
could impose substantial upload, readback and latency costs.

## Other recent leads and their actual scope

| Work | Finding and limit |
|---|---|
| [MSHV / direct virtualization, FOSDEM January 2026](https://fosdem.org/2026/schedule/event/BFQ8XA-introducing-mshv-accelerator-in-qemu/) | New QEMU acceleration through a Linux driver exposing Hyper-V. Direct virtualization delegates resources through an existing hypervisor. It is an alternative to KVM where that service exists, not a userspace replacement for unavailable hardware access in this lab. |
| [Kayfabe](https://github.com/reindertpelsma/kayfabe/blob/650661364db5adc7f0285d48f7ac607d9222260f/README.md), public repository created July 24, 2026 | Emulates NVIDIA hardware to run an unmodified guest NVIDIA driver while forwarding compute. Windows is a design goal; the inspected status table says Linux tested and no graphics/Vulkan/display. Its real-GPU setup uses KVM and its compute rewrite remains incomplete. Not a demonstrated Windows graphics solution. |
| [WinVisor, January 24, 2025](https://www.elastic.co/security-labs/blog/winvisor-hypervisor-based-emulator) | Windows x64 application analysis using Windows Hypervisor Platform. Requires a Windows hypervisor service; does not boot Windows from unprivileged Linux. |
| [Direct-translation research, January 6, 2025](https://arxiv.org/html/2501.03427v1) | A RISC-V 64-bit base-instruction user-mode prototype with synthetic benchmarks. Its reported maximum 35x improvement is not full x86 system emulation or a Windows/game result. |
| [DuVisor](https://arxiv.org/abs/2201.09652), 2022 | User-level hypervisor with a custom RISC-V hardware extension and kernel initialization. Interesting precedent for delegation; not a deployable rootless x86 route. |
| [WinBoat](https://github.com/TibixDev/winboat) | Its container wraps a Windows VM; normal prerequisites include KVM. The container presentation does not remove that requirement. |
| [Microsoft LiteBox](https://github.com/microsoft/litebox), inspected September 8, 2026 | Its documented uses include unmodified Linux programs on Windows and Linux sandboxing. It is not a Windows NT runtime for Linux. |

The search did not establish an available 2025–2026 implementation that runs
unmodified modern Windows at near-native CPU speed as an ordinary Linux
process without a host virtualization service or privileged driver.
This is a search finding, not an impossibility proof.

## A more ambitious CPU design

Another possible research direction is **direct execution of suitable Windows
user code** with a separately adapted kernel execution path. That adaptation
could be a source-level port, as in Drawbridge, or a much more difficult
binary transformation of kernel code and sensitive instructions. The Windows
kernel would remain the authority on guest OS semantics. Host mappings could
mirror selected guest user address spaces; syscalls, exceptions and interrupts
would transfer to that kernel implementation.

That would require correct handling of address-space changes, page-table
writes, protected/high kernel addresses, segmentation, TLS, exceptions,
self-modifying code, executable mappings, atomics and multiple CPUs. Ordinary
syscall interception handles only a fraction of this. The implementation also
needs isolation between directly executed code and its kernel execution state.

Software virtualization has historical precedent, but early VMware's hosted
architecture still installed a privileged host driver and VMM. Its performance
cannot be imported as evidence for a gVisor-only implementation.
[VMware authors' architecture paper](https://www.usenix.org/legacy/publications/library/proceedings/usenix01/sugerman/sugerman_html/node3.html).

This is a speculative design sketch, not a claim of novelty, a small patch,
an implementation plan with a known timeline, or a performance result.
An NT syscall personality in the Sentry would be a different approach: it
would reimplement Windows OS behavior, closer to application compatibility
than booting Microsoft's kernel. Our existing Wine route already addresses
part of that application-level problem.

## VR introduces a third requirement

A Windows desktop or working Direct3D benchmark would still leave headset
poses, controllers, swapchain sharing and compositor timing unresolved.
Windows applications need a Windows-compatible XR runtime or a deliberately
implemented bridge to the existing Linux runtime; pointing the Windows
loader at our Linux Monado shared library is insufficient.
[OpenXR loader design](https://registry.khronos.org/OpenXR/specs/1.1/loader.html).

Monado has a Windows port, including documented service and `hello_xr`
instructions. Its published limitations include missing actual headset
drivers. Our virtual-headset/controller and stereo-capture modifications would
need to be ported or bridged and tested with the virtual GPU.
[Monado Windows build](https://monado.pages.freedesktop.org/monado/winbuild.html).

At 90 FPS the frame interval is about 11.1 ms. A fast physical GPU alone does
not establish that the game thread and XR submission path can keep up. There
is no measured full-Windows frame rate in this lab.

The independent acceptance questions are therefore:

1. What concrete Windows kernel/runtime can execute here without CPU emulation
   or a host virtualization service, and does its measured CPU/I/O performance
   satisfy the application workload?
2. Can a Windows virtual GPU submit correct real GPU work through the Linux
   renderer and our device boundary, with verified memory and fence behavior?
3. Can a Windows XR workload accept controller/headset state and deliver
   correct stereo frames at useful measured latency and sustained frame rate?

No OS image, new Windows VM, GPU-driver change or runtime modification was
made during this research. Existing desktops and assets were preserved.

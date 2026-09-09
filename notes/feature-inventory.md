# Sandbox feature inventory

Inventory of the standalone lab, based on committed implementation and saved
acceptance through September 8, 2026. This inventory involved no environment
launches or new runtime tests. Historical notes sometimes describe an earlier
stage; the later acceptance linked here determines the current classification.

**Verified** means exercised in the documented lab configuration, not universal
application, hardware or production qualification. **Experimental** means an
opt-in or incomplete implementation with the stated limits. **Operational**
means an available workflow or helper, not a unified platform API.

## 1. Execution and deployment

| Feature | Status and scope |
| --- | --- |
| Linux execution without KVM | Verified: patched gVisor systrap, with `/dev/kvm` absent from the runtime container. |
| No host sudo or administrator changes | Verified on the cluster using unprivileged Apptainer/user namespaces and the existing host facilities. Guest root remains available. |
| CPU-only environments | Verified; GPU exposure is optional and CPU launches expose no NVIDIA devices. |
| GPU-enabled environments | Verified with one selected, allocated NVIDIA GPU per sandbox. Multiple sandboxes can run concurrently. |
| Command-only execution | Verified custom entrypoints: launch a program, shell or script instead of booting the full desktop/systemd workload. |
| Full service/desktop environments | Verified Ubuntu 22.04 userspace, systemd PID 1, services, D-Bus, user sessions and GNOME. |
| Native Apptainer execution | Operational CPU command wrapper and GPU application/control launcher. This path uses the host kernel directly; the gVisor lifecycle, resource and network APIs do not automatically apply. |
| Slurm deployment and GPU acquisition | Operational: use existing jobs or request fresh GPU jobs, including preempt QoS and model constraints; access allocated nodes through SSH. The environment API does not itself allocate Slurm jobs. |
| Multiple independent named environments | Verified separate writable state, process identities, logs and forwarded ports; concurrent workloads can share the allocation and GPU. This is not exclusive hardware reservation. |
| Detached execution | Available launcher mode keeps an environment independent of the invoking terminal, within the allocation's lifetime. |

Evidence: [reproduction](gvisor-lab-reproduction.md),
[lifecycle](environment-lifecycle.md), [GPU access](single-gpu.md),
[fresh allocations](preempt-vr-debug.md),
[native CPU command wrapper](../scripts/native-control-exec.sh),
[native GPU launcher](../scripts/run-native-gpu-app.py).

## 2. Linux services and containers

| Feature | Status and scope |
| --- | --- |
| Guest root, ordinary users and guest sudo | Verified inside the sandbox; these grant no host administrative authority. |
| Installed Linux applications and dependencies | Verified package/application installation and retained application profiles, home directories and system configuration. |
| systemd-managed services | Verified boot, service management and application-specific transient units. |
| Docker daemon and Compose inside the guest | Verified real Docker/containerd, container startup, persistent data and service composition. |
| Docker-in-Docker | Verified a second Docker daemon inside a guest Docker container, including Moodle's nested MariaDB workload. |
| Container network and storage semantics | Verified bridges, DNS, published ports, NAT/firewall rules, private cgroup namespaces and overlay-backed container storage in the accepted layout. These are guest facilities, not host cgroup delegation. |
| Guest filesystem metadata | Verified ownership, permissions, ACLs, xattrs/capabilities, links and persistent mount data; supported EROFS features and cold-snapshot metadata have explicit tests. |

Evidence: [reproduction and Docker export](gvisor-lab-reproduction.md),
[whole-environment acceptance](resource-snapshot-implementation.md),
[filesystem round trips](gpu-filesystem-snapshots.md).

## 3. Lifecycle, checkpointing and saved environments

| Feature | Status and scope |
| --- | --- |
| Start, status and list | Python API and JSON CLI; start waits for the runtime, with application readiness checked separately. |
| Pause/resume in place | Verified for CPU and GPU environments, including resident CUDA/OpenGL resources. Keeps RAM, ports and device contexts; does not release the allocation or freeze external peers/previously submitted GPU work. |
| Save while preserving the source | Verified; after capture the source returns to its prior running/paused state. |
| Save-before-stop | Default stop publishes a snapshot before teardown. A failed save preserves the source. Explicit discard is available. |
| Full CPU checkpoints | Verified RAM, processes, writable files, file descriptors/offsets, deleted-open files, queued Unix IPC, nested namespaces, internal established TCP and firewall/NAT state. |
| Restore into independent clones | Verified loading a saved state into a new name; later changes to the source do not alter the clone. Re-checkpointing and restoring an already restored environment also passed. |
| Persistent filesystem snapshots | Verified CPU/GPU cold restore of the writable root and separate persistent Docker/containerd mounts, application installs, profiles, projects and configuration. Starts fresh processes; application RAM and live GPU contexts are excluded. |
| CUDA-aware live checkpoints | Experimental opt-in save and restore; a small CUDA control passed. Live graphics restore did not pass. |
| Snapshot integrity verification | Automatic asynchronous checksums after publication, verification status, explicit full recheck and restore-with-verification. Known verification failure blocks a new restore. Normal restore uses metadata checks and may precede checksum completion. |
| Faster local restore path | Verified reuse of an unchanged local frozen capture, with persistent-storage fallback. No incremental checkpoint chain. |
| Exact runtime/dependency preservation | Immutable runtime builds and snapshot manifests pin the runtime, base and required fixtures. A snapshot needs its recorded dependencies when moved. |
| Controlled runtime selection | Per-launch immutable build selection; experimental current-runtime substitution for cold filesystem restore. Live snapshots retain their recorded runtime. |
| Coordinated lifecycle and cleanup | Per-environment locks, idempotent pause/resume/stop, refusal to replace existing names, owned-process cleanup and PID identity checks. |

External services do not roll back with a checkpoint and may require
reconnection. Filesystem-only cuts are crash-consistent, not application-specific
transactional backups. Pausing is distinct from saving and stopping.

Evidence: [lifecycle API](environment-lifecycle.md),
[full snapshots](resource-snapshot-implementation.md),
[GPU/filesystem snapshots](gpu-filesystem-snapshots.md),
[verification and local reuse](asynchronous-snapshot-verification.md),
[runtime selection](preempt-vr-debug.md#harness-changes).

## 4. Resource controls

| Feature | Status and scope |
| --- | --- |
| CPU pool/affinity selection | Select eligible allocated CPUs without reserving them exclusively. |
| Configurable guest CPU count | Advertise/control guest execution parallelism independently of the host pool. |
| Weighted CPU sharing | Verified environment-level sharing with configurable weights and borrowing of idle capacity. |
| CPU-equivalent quotas | Verified sampled average quota control; optional unrestricted shared scheduling also exists. These are userspace policies, not host kernel `cpu.max`. |
| Guest memory budget | Verified allocator limit covers anonymous guest memory and writable tmpfs/overlay data; rejected allocations do not require stopping unrelated environments. |
| Runtime/helper memory guards | Separate sampled Sentry runtime guard and transport-helper address-space limits. These do not form a hard aggregate host RSS limit; overshoot and unaccounted categories are documented. |
| Allocated GPU selection | Checks allocation membership, device identity and matching driver; exposes the selected device/control nodes. |
| GPU sharing | Concurrent environments can use the same physical GPU, including alongside host workloads. No ordinary per-environment graphics throughput or VRAM guarantee. |
| CUDA compute partitions | Experimental MPS static SM partitions across cooperating environments; four/eight-SM clients and lifecycle cleanup tested on L40S. |
| CUDA client memory ceiling | Experimental MPS per-client allocation limit with rejection verified. It is not an aggregate sandbox VRAM limit. |

MPS does not isolate graphics, memory bandwidth or all GPU monitoring information.
MPS snapshots are deferred. The installed driver and GPU combinations require
qualification; the lab uses an explicit allowance for its upstream-unqualified
NVIDIA driver ABI.

Evidence: [resource controls](resource-snapshot-implementation.md),
[device selection](single-gpu.md), [sharing limits](gpu-sharing-partitioning.md),
[experimental MPS](experimental-gpu-mps.md).

## 5. Networking and access

| Feature | Status and scope |
| --- | --- |
| Internet-enabled mode | Public IPv4 and DNS egress under an outside-guest policy. |
| Offline mode | Blocks outgoing internet/DNS while retaining incoming forwarded control, SSH, web and desktop sessions. Verified with desktop services and GunSpinning. |
| Destination restrictions | Blocks host/private/link-local and cross-environment destinations at the external boundary; explicit additional CIDRs are supported without overriding host-address denial. |
| Internal container networking | Docker-to-Docker communication remains inside the guest virtual network, including in offline mode. |
| Host port forwarding | Loopback TCP mappings for SSH, VNC and guest web services, with per-environment port discovery. |
| Remote terminal and desktop access | Guest exec/SSH and VNC through SSH tunnels; host node SSH is a separate operational access path. |

The external policy is IPv4-only; unsupported IPv6, VLANs, fragments and IP
options are rejected. Functional policy tests are not a complete security audit.

Evidence: [network policy and restore](resource-snapshot-implementation.md),
[access and ports](gvisor-lab-reproduction.md),
[offline game acceptance](gunspinning-vr-experiment.md).

## 6. Desktops, graphics and media

| Feature | Status and scope |
| --- | --- |
| Headless desktop hosting | Ubuntu/GNOME on a cluster without an attached display, accessed through VNC. |
| X11/Xvnc desktop backend | Verified default, with CPU software rendering and NVIDIA-rendered application presentation. |
| NVIDIA OpenGL/EGL/GLX | Verified through the configured VirtualGL path, including interactive browser and desktop applications. |
| NVIDIA Vulkan rendering/presentation | Verified Monado and game rendering; the tested Xvnc game path uses Primus-VK with software display delivery. Not arbitrary native Vulkan presentation support. |
| CUDA/OpenGL interoperability | Verified shared-buffer writes/readback and application use. |
| GNOME Wayland/Xwayland option | Experimental GPU compositor with Xwayland SHM and VirtualGL-rendered Open Saber. Native GPU Wayland application presentation remains unresolved; no overall speedup established. |
| Desktop audio service and audio-containing exports | Verified Resolve's virtual audio backend and exported stereo PCM. VNC does not stream that audio to the remote viewer; VR sound/haptics remain unvalidated. |

Evidence: [GPU graphics](single-gpu.md), [Earth](gpu-applications.md),
[Resolve](resolve-gpu.md), [Wayland](wayland-runtime-experiment.md),
[Vulkan game path](gunspinning-vr-experiment.md#execution-paths).

## 7. Computer-use observation and control

| Feature | Status and scope |
| --- | --- |
| Fast desktop screenshots | Fresh CPU-readable images through host-shared memory, bypassing VNC image encoding/transport. Works with CPU desktops and NVIDIA-rendered content presented to Xvnc. |
| Python/CLI image access | Owned PIL RGB images, optional cached capture, PNG/JPEG export, resize handling and frame metadata. Current root capture excludes the separate hardware cursor. |
| Mouse control | Movement; left/middle/right/back/forward clicks; double/triple clicks; drag paths; explicit button states; vertical/horizontal/diagonal scrolling. |
| Keyboard control | Text, key chords and explicit key-down/up states; qualified Unicode/emoji input with reusable mappings and per-batch limits. |
| Ordered action batches and step | Persistent input connection, validation, X-server acknowledgement and action-then-screenshot. ACK does not establish application repaint completion. |
| Lifecycle-aware I/O | Detach before pause/save/stop and reconnect after resume/restore, including tested CPU live and GPU filesystem restores. |
| Computer-use harness adapter | Canonical action translation; the recorded final audit passed 100/100 cases and 500/500 repetitions. This is not integration of the complete Gym Anything task suite. |

The fast desktop API is Xvnc-only. The experimental Wayland desktop has diagnostic
capture but no equivalent qualified fast-I/O backend. Unicode qualification
covers tested toolkit event loops rather than all possible applications.

Evidence: [fast desktop I/O](xvnc-fast-io.md),
[action coverage and audit](fast-io-action-fixes.md).

## 8. VR and game interaction

| Feature | Status and scope |
| --- | --- |
| Headset-free XR runtime | Experimental patched Monado runs real game XR submissions against a virtual headset and controllers. Physical headset delivery is not established. |
| Native OpenXR games | Open Saber gameplay verified. |
| OpenVR/SteamVR API compatibility | xrizer translates supported OpenVR calls to OpenXR; native GunSpinning tracked-controller gameplay works without Steam. This does not remove another game's Steam/login/DRM requirements. |
| Virtual head and both controller poses | Programmatic position/orientation and protocol fields for velocities, sticks, trackpads, analog, button/touch and finger values. Actual field use is game-dependent; not every protocol field has gameplay acceptance. |
| Persistent acknowledged VR input | Validated state updates, matching Monado acknowledgement and recorded input timing; ACK indicates state installation, not game processing. |
| Userspace virtual gamepad | SDL proxy provides joystick/buttons/axes without a physical USB, evdev or uinput device. GunSpinning menu selection, aim, fire and reload verified; analog-trigger mapping remains unresolved. |
| Simultaneous eye observations | Real left/right compositor views from one frame, with shared frame ID/timestamps and separate eye access. |
| Continuous stereo stream | Bounded, size-sealed shared-memory ring; owned client images, latest-frame access and explicit skipped-frame accounting. |
| Optional desktop mirror | XR capture is separate from the flat game mirror; mirror selection can be varied without redefining the XR interface. |
| Game-independent stream ownership | The stream can attach to a caller-launched game instead of always launching Open Saber; exercised by the GunSpinning demo. |
| Offline game operation | Native GunSpinning gamepad and tracked-controller modes launched and accepted controls with egress blocked and assets already staged. |

Evidence: [Open Saber](vr-monado-experiment.md),
[VR input/stream API](vr-continuous-io.md), [paired eyes](vr-stereo-io.md),
[GunSpinning](gunspinning-vr-experiment.md).

## 9. Recording, diagnostics and reproducibility

| Feature | Status and scope |
| --- | --- |
| Lossless stereo recording | Background Zstandard-compressed paired frames with frame index, capture timestamps, bounded queues and explicit recording-drop/error accounting. |
| Both-eye demo videos | Separate left and right videos plus synchronized side-by-side preview, retaining capture timing. Actual exports are decoded and sampled visually for acceptance. |
| Offline recording access | Read retained frames and export videos without restarting the game or sandbox. |
| Partial equirectangular conversion | Offline reprojection from eye videos plus explicit FOV; unknown directions remain black. It cannot recover full 180/360-degree coverage or new viewpoints. |
| Latency and frame-rate instrumentation | Desktop input/capture metadata, VR input/observation logs and Monado application/compositor/GPU metrics. Reports distinguish capture cadence, application submissions and reused frames. |
| Saved logs and acceptance probes | Runtime/application logs, structured results, source/device hashes, focused reproductions and native comparison launchers. |
| Resumable asset-import helper | Operational manifest-driven large-file import with partial-file resume and bounded workers, exercised for game assets. This is specialized tooling, not a generic storage service. |
| Versioned builds and recovery bundles | Committed scripts/patches, independent engine history, immutable runtime builds, dependency manifests and verified reconstruction evidence. Recovery archives supplement Git and runtime snapshots. |

Evidence: [recording](vr-continuous-io.md),
[both-eye video acceptance](gunspinning-vr-video.json),
[offline projection](vr-offline-equirectangular.md),
[source pins](source-revisions.json),
[recovery evidence](resource-snapshot-status.json),
[asset importer](../scripts/import-alyx.py).

## 10. Application workflows already exercised

These demonstrate the capabilities above; they are not additional promises that
every application feature or every combination of environment options works.

| Application/workload | Verified workflow |
| --- | --- |
| Firefox | Interactive browsing/input, normal browser sandboxing, logged-in Moodle use, NVIDIA WebGL/WebRender, profile/filesystem restore and CPU live desktop restore. |
| Moodle + MariaDB | Web service, course/login/navigation, actual nested Docker database, persisted data and whole-environment restore. |
| Google Earth Pro | NVIDIA globe rendering, typed search and navigation, plus cold-restored settings. A shutdown crash also reproduced in the native control. |
| DaVinci Resolve | Import, timeline playback, color grading, save/reopen, and a five-second 1080p ProRes export with audio; the tested project reached 24 fps. Hardware video-codec acceleration was not established. |
| PyTorch/CUDA | Backpropagation, optimizer updates and model/optimizer file save/load; a 21.5M-parameter synthetic comparison measured about 99% of native steady-state steps/s. This is one workload, not a general performance guarantee. |
| Open Saber | Real VR gameplay, virtual head/controllers, desktop mirror and recorded stereo output on the tested L40S and RTX PRO 6000 configurations. |
| GunSpinning VR | Official native Linux build in gamepad and tracked-controller modes; offline scene loading, aiming, firing and reloading. An L40S sample measured about 61 application submissions/s; recording cadence is reported separately. |

Evidence: [desktop/full-state acceptance](resource-snapshot-implementation.md),
[GPU applications](gpu-applications.md), [Resolve](resolve-gpu.md),
[training](single-gpu.md), [hardware follow-up](preempt-vr-debug.md),
[GunSpinning](gunspinning-vr-experiment.md).

## Boundaries: not available as completed features

- A booted Windows VM. Selective execution of five Microsoft kernel routines is
  a mechanism experiment; Windows work is paused. Wine/DXVK startup is a separate
  compatibility path, not a Windows VM.
- General Steam compatibility: its 32-bit Linux bootstrap is blocked by the
  current 64-bit executable loader. Alyx reached stereo menus but level gameplay
  failed; AMS2 reached a Steam sign-in error, without verified racing gameplay.
- Live graphics checkpoint/restore, MPS snapshots, incremental checkpoints,
  automatic checkpoint-on-preemption, or transparent live migration.
- Multi-GPU operation inside one sandbox, dedicated graphics partitions, hard
  aggregate environment VRAM limits, or host-cgroup-equivalent CPU/RSS limits.
- Native GPU Wayland application presentation or Wayland fast desktop I/O.
- Physical headset scanout/tracking, end-to-end motion-to-photon performance,
  VR audio/haptics, direct GPU-tensor observations, or full panoramic capture
  reconstructed from the existing forward eye videos.
- Arbitrary Linux/kernel-module/device compatibility, universal GPU/driver
  support, production-scale reliability, or
  complete Gym Anything environment/task integration. The broader VR, robotics
  and embodiment objective remains open beyond the workloads exercised here.

See [Windows mechanism experiments](windows-native-kernel-experiments.md),
[Steam client](steam-client-experiment.md),
[game startup limits](preempt-vr-debug.md), and the feature-specific notes above.

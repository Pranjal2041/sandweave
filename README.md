# General VM: no-KVM Slurm lab

Current runtime: patched **gVisor systrap**, running in unprivileged Apptainer with no KVM access, host sudo, or administrator changes. This independent lab retains a Linux desktop, guest root, systemd, and actual nested Docker. Gym Anything's runtime code remains untouched.

Moodle 4.5.13 with Docker inside Docker and MariaDB, Firefox 155, and Google Earth Pro 7.3.7 have been exercised. The user requested closing those desktops. The current fixed desktop is `resolve-optfix`, running GPU-accelerated DaVinci Resolve 21.0.4 at **24 fps** on the tested project, up from 8.9 fps after repairing GPU completion notifications. Import, color grading, project reopening and ProRes export also passed. The original `resolve-gpu2` remains available. This is a lab compatibility result; the project's complete environment/task suite and an actual node without a KVM device remain untested.

## Four controls implemented and tested

| Area | Current behavior |
|---|---|
| CPU | Weighted sharing by default; idle capacity can be borrowed. Optional average CPU-equivalent quotas. Affinity selects eligible CPUs, without exclusive ownership. |
| Memory | Guest page allocator budget; separate sampled Go-runtime guard; address-space limits for network helpers. This is not an aggregate host-cgroup RSS cap. |
| Network | Outside-guest policy allows public IPv4 while denying host/private/cross-environment destinations. Offline mode keeps incoming forwards and blocks egress. Nested Docker networking remains available. |
| Snapshots | CPU environments: whole running environment save/restore, including RAM, processes, writable files, open descriptors, IPC, nested namespaces, firewall/NAT state and internal TCP. GPU environments: persistent-filesystem cold restore is supported; RAM + processes + CUDA is experimental; live graphics restore is unsupported. |

Defaults for new launches: 4 advertised guest CPUs, 8 GiB guest-page budget, 1 GiB runtime guard, weight 100, and the inherited Slurm CPU allocation as the shared pool. CPU control is sampled userspace scheduling; the runtime guard permits transient overshoot. The Resolve desktop overrides the guest-page budget to 48 GiB.

Read [implementation, measurements and limits](notes/resource-snapshot-implementation.md), [machine-readable status](notes/resource-snapshot-status.json), and [reproduction instructions](notes/gvisor-lab-reproduction.md).

[Xvnc fast I/O](notes/xvnc-fast-io.md) is implemented and tested with CPU GTK and
NVIDIA-accelerated Firefox: persistent acknowledged input plus direct host-shared
screenshots. At 1280×800, fresh RGB captures measured roughly 5–6 ms median;
GPU 1080p tail latency remains above 10 ms. Pause/save detaches the shared buffer
and resume/load reconnects it. Use the `scripts/fastio.py` CLI or
`EnvironmentManager().fast_io(ENV)`.
[The cua-auto-harness audit](notes/cua-harness-fast-io.md) records canonical
action coverage and reference comparisons. [The action repairs](notes/fast-io-action-fixes.md)
add back/forward buttons, horizontal/diagonal scrolling and reusable Unicode
mappings, with explicit synchronization and per-batch limits. The full rerun
passes **100/100 cases and 500/500 repetitions**, with no unsupported cases.
[Fast-I/O research](notes/fast-io-research.md) records the preceding source analysis.
[Display architecture research](notes/display-architecture-research.md) compares
Xvnc with headless GNOME/Mutter and records the host render-node capabilities
and gVisor device support needed for a GPU compositor.
[The Wayland source investigation](notes/wayland-source-investigation.md)
identifies upstream headless-session fixes, NVIDIA DMA-BUF/timeline operations,
and the missing engine and packaging integration. [The Wayland runtime experiment](notes/wayland-runtime-experiment.md)
now verifies headless GNOME GPU composition and Open Saber through Xwayland SHM
with the existing VirtualGL renderer, controller input and stereo recording.
Native GPU Wayland presentation remains unresolved, and no overall speedup is
established. **Xvnc is the default for future testing.** Wayland remains an
explicit experimental option through `scripts/wayland-lab.py` and the VR stream's
`--x11-display` / `--xauthority` arguments. Controller and Monado/XR support are
separate capabilities.

[The first VR experiment](notes/vr-monado-experiment.md) runs the published
Open Saber game through Monado with a virtual headset and controllers, using
the existing gVisor GPU sandbox. Gameplay and a composed eye image were verified.
The sampled game rate was 90 FPS with a 120 Hz compositor target and continuous
desktop readback disabled; the live Xvnc mirror configuration measured 61 FPS.
This uses an explicit experimental Monado patch. Physical headset delivery,
audio and virtual haptics remain unvalidated or unsupported.

[The Alyx startup experiment](notes/alyx-startup-experiment.md) reuses an existing
Windows installation through Wine, DXVK, xrizer and Monado. All assets are now
imported and readable inside the sandbox. The
[fresh-allocation investigation](notes/preempt-vr-debug.md) reaches the stereo
main menu and accepts controller input on L40S and RTX PRO 6000. L40S level
loading still fails, with evidence of host memory-mapping exhaustion. No Alyx
level gameplay is established. Recovery of the earlier NVIDIA wait is untested.

[The Automobilista 2 Demo experiment](notes/ams2-demo-experiment.md) has acquired
and fully imported the free 2026 demo through the user's Windows Steam. A
fresh RTX Xvnc/GPU/Monado sandbox passed the splash and rendered a stereo
sign-in error stating that Steam is not running. Racing gameplay remains
unverified; the earlier host NVIDIA wait is a separate unresolved observation.
The startup investigation also exposed a Wine exception-classification gap;
the engine repair passes Windows fault recovery and a live CPU restore test.
The [Linux Steam client check](notes/steam-client-experiment.md) found a separate
startup blocker: its 32-bit bootstrap is rejected by the current 64-bit-only
gVisor executable loader. Alyx itself has a native Linux build; its earlier
Linux launch stopped at Steam initialization.
The [standalone Linux VR shortlist](notes/standalone-linux-vr-games.md) records
free game candidates, their Linux/XR evidence, and outstanding offline/runtime
checks; Open Saber remains the only game on that list tested in this lab.
The [Windows-without-KVM investigation](notes/windows-without-kvm-research.md)
examines fast Windows execution, Drawbridge's user-mode NT precedent, the new
Helios GPU stack, and separate CPU, graphics and XR requirements. The
[native Windows kernel experiments](notes/windows-native-kernel-experiments.md)
now execute five actual Microsoft kernel routines inside gVisor with selective
memory-access rewriting. A 10,000-operation differential tree check passes;
the selected tree benchmark measures about 5.5% overhead. This is a component
result: Windows has not booted, and VM/game performance remains unestablished.
KVM and TCG remain excluded.

[Continuous VR I/O](notes/vr-continuous-io.md) delivers paired left/right eye
images to a host Python client through a bounded, size-sealed shared-memory ring,
while accepting persistent acknowledged controller/headset state. Each pair has
one compositor frame ID and timestamp. Both eyes are recorded losslessly, and
the viewing video packs them side by side. [Stereo measurements and API examples](notes/vr-stereo-io.md)
document the tested path; the earlier monocular measurements remain in the history.

## Pause, resume and stop

Use `python scripts/env.py pause ENV`, `resume ENV`, or `stop ENV`.
Pause retains the resident environment; stop saves a snapshot before terminating.
CPU environments default to a live snapshot, GPU environments to a filesystem
snapshot. `stop ENV --discard` explicitly skips saving. `save ENV LABEL` and
`load snapshots/LABEL NEW_ENV` expose the same snapshot implementation.
The [lifecycle API](notes/environment-lifecycle.md) documents the Python interface,
start/status/list commands, GPU limits and tested cleanup behavior.

## Restore the validated desktop

```bash
cd ~/scratch/general-vm
python scripts/run-gvisor.py --detach --restore snapshots/full-desktop-ready my-desktop
```

The launcher checks snapshot sizes and recorded base/runtime metadata, restores its settings, and allocates fresh loopback ports. Normal restores do not scan payloads for checksums. If an unchanged frozen capture is still available on this node, the launcher reuses it instead of reading the persistent copy. `runs/gvisor/my-desktop/ports.json` identifies VNC port 5901's host mapping. Connect through an SSH tunnel; the lab VNC password is `labvnc01`. The snapshot contains a logged-in Moodle course in Firefox and a running nested database.

Save another complete running environment with:

```bash
python scripts/checkpoint-gvisor.py my-desktop my-checkpoint
```

Snapshots are durable under `snapshots/`, with shared immutable dependencies under `images/` and `tools/runtime-builds/`. Preserve every dependency listed in `snapshot-manifest.json` when moving them. External peers do not roll back with the snapshot; applications must reconnect external sessions. Local internal TCP is included and tested.

Save returns after publication and starts checksum verification in a detached, lower-priority worker. Check `snapshots/my-checkpoint/verification.json` for `pending`, `running`, `passed`, or `failed`. Restore can proceed while verification is pending/running; a known failure blocks new restores until a successful recheck. Keep the local capture until initial verification passes, including before moving the checkpoint to another node.

For explicit verification, use `python scripts/verify-snapshot.py snapshots/my-checkpoint`, or add `--verify` to a restore to wait for full verification first. [Timing breakdown and verification behavior](notes/asynchronous-snapshot-verification.md) document the checks and live measurements. Incremental checkpoints remain deferred.

## Single-GPU experiment

The lab can expose one allocated NVIDIA GPU through gVisor `nvproxy`, still using
systrap without KVM or host sudo. PyTorch GPU training, interactive Firefox
WebGL/WebRender, Google Earth Pro rendering/search, and CUDA/OpenGL buffer sharing
passed on this node's L40S and driver 610.43.02. Earth has a recorded shutdown
crash requiring further investigation. Resolve's CUDA/OpenGL processing and a
five-second 1080p ProRes export passed; games are not yet tested.
GPU environments now support whole persistent-filesystem snapshots and cold
restore, including separate Docker storage mounts. Use
`python scripts/checkpoint-gvisor.py --filesystem ENV LABEL`, then the normal
`run-gvisor.py --restore` command. **RAM + processes + CUDA snapshots are
experimental**, requiring `--experimental-gpu-live` for both save and restore.
Only a small CUDA control passed; broader CUDA workloads need validation.
Firefox/Earth/Resolve live graphics snapshots failed and remain unsupported.
See [GPU filesystem snapshots and live-state evidence](notes/gpu-filesystem-snapshots.md).

See [GPU setup, evidence, measurements and limitations](notes/single-gpu.md).
The [sharing and partitioning investigation](notes/gpu-sharing-partitioning.md)
records cross-environment monitoring visibility and the initial native MPS test.
Opt-in [experimental CUDA MPS partitions](notes/experimental-gpu-mps.md) now work
across environments. Add `--experimental-gpu-sm-chunks 2` to a `--gpu 0` launch
for eight SMs on this L40S; optionally add
`--experimental-gpu-client-memory-mib 1024` for a per-CUDA-client memory limit.
Two concurrent guests, CUDA kernels, allocation rejection and lifecycle cleanup
passed. These are cooperative CUDA controls; graphics and memory bandwidth stay
shared, and MPS snapshots remain deferred. Ordinary GPU launches keep their
existing behavior.
The [GPU application investigation](notes/gpu-applications.md) records Earth's
VirtualGL fixes and interactive checks. [Resolve setup and acceptance](notes/resolve-gpu.md)
records the futex engine fix, audio setup, export verification and performance limits.
The [GPU notification repair](notes/resolve-gpu-notifications.md) records the
playback fix and native/before/after measurements. The fixed desktop is forwarded
to Mac VNC `127.0.0.1:5913`, password `labvnc01`.

## Recovery and history

This lab is tracked on branch `experiment/no-kvm-slurm`. Scripts, tests, notes,
patches and source probes belong in Git; generated data and downloaded dependencies
are covered by `.gitignore`. The engine has its own checkout in `sources/gvisor`;
[source revisions](notes/source-revisions.json) pin its exact commit and cumulative
patch. Check both repositories for a clean working tree when completing changes.

The CPU snapshot implementation recovery bundle is `checkpoints/async-snapshots-8c8b143/`. It records source, exact binaries, launch fixtures, scripts, documentation, checksums and test evidence. It is distinct from a frozen running snapshot. The earlier implementation remains at `checkpoints/resources-snapshots-8c8b143-r3/`, and the baseline at `checkpoints/baseline-ae303ca/`.

[Detailed experiment history](notes/gvisor-prototype-progress.md) preserves both failures and successful trials. [Historical UML README](notes/uml-readme-history.md) records the earlier, paused UML work; it is not the current launch path.

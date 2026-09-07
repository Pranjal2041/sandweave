# GPU sandboxes: filesystem snapshots and live-state experiments

Implemented and tested on `babel-u5-28`, Slurm job 10333558, without KVM or
host sudo. GPU: the allocated `/dev/nvidia0` (host NVML index 3), NVIDIA L40S,
driver 610.43.02. MPS was disabled throughout.

**Supported result: cold restore of the complete persistent filesystem.**
The installed applications, home directories, system configuration and separate
Docker storage mounts survive. Restore starts fresh processes. Live graphics
restore did not pass; pure CUDA live restore did pass a small control experiment.

## Commands

From `~/scratch/general-vm`:

```bash
python scripts/checkpoint-gvisor.py --filesystem ENV LABEL
python scripts/run-gvisor.py --detach --restore snapshots/LABEL NEW_ENV
```

Use a fresh environment name. `--restore` detects the snapshot kind and restores
its GPU allocation, resource settings and network policy. A filesystem restore
boots `/sbin/init`; an explicit command after `NEW_ENV --` overrides that boot
command. It does not rerun installation or Docker-import commands from the
original launch. Docker containers restart according to their normal restart
policy; containers without one need `docker start` after the cold boot.

The validated durable example is `snapshots/fs-gpu-supported-save`. It contains
Resolve 21.0.4, a Resolve project and media, the Firefox profile, Earth settings,
Moodle and nested MariaDB storage. Its initial background verification passed.
`runs/gvisor/NEW_ENV/filesystem-ready.json` marks completion of filesystem mount
reconstruction, **before** application services necessarily become ready.

The default uses the snapshot's recorded runtime. The explicit experimental
`--filesystem-runtime-current` option tests a cold archive on the staged runtime;
it is rejected for RAM/process snapshots. This was used to validate the final
mount-replacement repair, then a new snapshot was taken on that runtime and
restored normally.

## What is saved

The root is an immutable EROFS base plus a writable tmpfs overlay. We export the
underlying upper filesystem, including files hidden by the live Docker mounts,
whiteouts and opaque-directory attributes. This is not a walk of selected
application directories or a `docker export` operation.

The paused init mount namespace is inventoried. Every configured persistent
tmpfs mount is exported separately. In the Docker-import layout these are
`/var/lib/docker` and `/var/lib/containerd`; nested Docker's backing data lies in
that captured storage. Docker's runtime overlay mounts are reconstructed at boot.
Unexpected writable mounts and stacked persistent mounts are rejected instead
of silently producing an incomplete snapshot.

As with a disk snapshot followed by a power cycle, `/run`, `/tmp`, `/dev`
(including `/dev/shm`), `/proc` and `/sys` are recreated. Socket endpoints, open
but unlinked files, process memory, nested process/mount namespaces and GPU
contexts are not part of this cold snapshot. Files buffered only in application
RAM are not captured. The cut is crash-consistent, not an application-specific
transactional backup. Read-only inputs remain dependencies: preserve the EROFS
base, recorded runtime and staged GPU toolchain (`tools/gpu`, exposed read-only
at `/opt/engine-gpu`); the staged NVIDIA libraries must match the host driver.
The snapshot is not a self-contained hardware-independent image.

Filesystem restore reinstates each persistent tmpfs **before PID 1 starts
systemd or the requested application**. The initial empty mount is detached and
replaced, retaining its options, so repeated snapshots do not accumulate hidden
mounts. A failed mount reconstruction prevents application boot. Capture resumes
a previously running environment even on rejection, while an environment that
was already paused remains paused.

## Engine changes and checks

The existing `runsc tar rootfs-upper` command now accepts a filesystem-root
`--path`, and a host-controlled `--restore-mount` mode for cold-boot staging.
The tmpfs tar path needed three repairs before it could handle whole installed
environments:

- Preserve binary and empty xattrs and names containing `=`. Binary values use
  normal SCHILY PAX records; empty values and unrepresentable names use a
  validated base64 `GVISOR.xattr.*` extension. Existing ACL encoding is retained.
- Force PAX timestamp precision and restore recorded access/change times when
  present. The original automatic tar format rounded modification times.
- Stream file contents into guest pages instead of buffering an archive on the
  Go heap. Export uses bounded chunks too. Ordered exports advertise
  `GVISOR.tmpfs.stream=1`; legacy unordered input still has its existing reader.
  Zero chunks remain sparse on restore. Archives themselves currently store
  zero ranges densely, and exact extent layout/inode numbers are not preserved.

Live acceptance covered ordinary content, uid/gid/mode, nanosecond mtime,
capabilities, POSIX ACLs, binary/empty/`=`-named xattrs, hard links, symlinks,
FIFOs, sparse-file contents, deleted base files and opaque directories.
After changing the original environment, the restore retained the saved values.
Both separate Docker mounts retained their markers. Moodle returned HTTP 200
and nested `moodle-mariadb` restarted. A second filesystem snapshot of the
restored environment also restored and passed the metadata checks. An unexpected
writable mount was rejected and the original continued running; an already
paused source stayed paused after saving. A separate known-base-file whiteout
was confirmed as a 0:0 character entry in the archive and survived cold restore.
A subsequent **live CPU snapshot of that cold-restored guest** also restored
successfully, exercising preservation of its actual root-upper initialization
archive instead of the small launch fixture tar. See the
[additional round-trip results](gpu-evidence/fs-edge-results.json).

Visual acceptance reopened the GPU applications from the cold-restored files:
[Earth navigation](gpu-evidence/fs-earth-navigated.png),
[Firefox WebGL](gpu-evidence/fs-firefox-webgl.png), and
[Resolve's saved project](gpu-evidence/fs-resolve-project.png).
Earth responded to dragging, Firefox rendered its animated NVIDIA WebGL test,
and Resolve reopened the project and its timeline responded to playback.
These demonstrate fresh application startup, not live-context resume.

Checks passed: `test-filesystem-snapshot.py`, `test-snapshot-store.py`,
`test-gvisor-gpu.py`, Python compilation, and the engine's
`//pkg/sentry/fsimpl/tmpfs:tmpfs_test`, including an export/import/export test.

## Storage and timing

The EROFS base is shared; each snapshot saves the current writable upper plus
persistent mount archives. This has base-plus-delta space savings, but is not a
chain of incremental checkpoints: two snapshots duplicate their changed files.
The source resumes before publication. Hashing remains asynchronous after the
persistent copy is published, and normal restores do not hash payloads.

Measured with the installed Resolve/Docker environment (about 10.4 GiB):

| Phase | First durable save | Save after cold restore |
| --- | ---: | ---: |
| Root upper export | 5.07 s | 4.47 s |
| Docker storage export | 1.22 s | 0.97 s |
| containerd storage export | 2.82 s | 3.17 s |
| Total capture/pause interval | 9.62 s | 9.18 s |
| Persistent publication | 34.85 s | 16.38 s |
| Total save return | 44.72 s | 25.78 s |

The first cold restore took 0.51 s of launcher setup and 14.54 s from runtime
spawn to completed filesystem reconstruction. Normal service startup follows.
Publication varies with shared-storage/cache conditions; these are observations,
not fixed latency promises. See [recorded measurements](gpu-evidence/fs-snapshot-results.json).

## Live RAM/process/GPU attempts

We used NVIDIA's utility version 610.43.02, pinned to repository revision
`00d5cce84c628088d6caa203fc4af40c1538b6f7`. Reinstall it with:

```bash
python scripts/stage-cuda-checkpoint.py
```

NVIDIA documents a CUDA-specific suspend/resume mechanism: it drains CUDA work,
copies CUDA allocations into host memory and later restores CUDA resources.
gVisor integrates that mechanism around its own process/kernel checkpoint.
Sources: [NVIDIA utility](https://github.com/NVIDIA/cuda-checkpoint/blob/00d5cce84c628088d6caa203fc4af40c1538b6f7/README.md),
[gVisor GPU checkpointing](https://gvisor.dev/docs/user_guide/checkpoint_restore/#gpu-checkpointrestore).

| Workload | Result |
| --- | --- |
| Small CUDA allocation + live RAM state, gVisor | Save and restore into a second sandbox passed: same guest PID 742, RAM nonce, GPU virtual address and 4096 bytes of GPU data; request counter continued from the saved value. |
| Live OpenGL context, gVisor | Checkpoint rejected a remaining graphics address-space object (`FERMI_VASPACE_A`, class `0x90f1`). |
| Live registered CUDA/OpenGL buffer, gVisor | Same unsupported graphics-object boundary; no successful live restore. |
| Firefox, Earth and Resolve with project open | All three checkpoint attempts rejected remaining graphics objects of class `0x90f1`. |
| Native Apptainer CUDA-only control | Lock/checkpoint/restore/unlock all passed; memory address, contents and RAM nonce retained. |
| Native Apptainer CUDA/OpenGL interop control | Lock/checkpoint succeeded, graphics device FDs remained open, and restore failed with `invalid argument`; unlock then failed because of the process state. |

The native controls used the same namespace setup, GPU, driver and utility
invocation method. This isolates the interop failure from gVisor's serialization
check. The `--hold-gl` and `--hold-interop` modes in `gpu-glx-interop-probe.py`
retain and repeatedly validate real graphics resources instead of exiting
before the snapshot. `gpu-checkpoint-cuda-probe.py` provides the positive control.
[Native control results and exact rejection lines](gpu-evidence/gpu-live-snapshot-results.json)
are tracked alongside the source probes.

Merely allowing `0x90f1` through the serializer would not save the graphics
address space, its mappings, allocations or execution state. The failed native
interop restore also remains outside that change's reach. This is a failure of
the tested graphics checkpoint path, not proof that graphics checkpointing can
never be built. No general training, UVM, Vulkan or MPS snapshot support is
claimed from the small CUDA control.

For disposable CUDA-only investigations, both operations require the explicit
`--experimental-gpu-live` flag:

```bash
python scripts/checkpoint-gvisor.py --experimental-gpu-live ENV CUDA_LABEL
python scripts/run-gvisor.py --detach --experimental-gpu-live \
  --restore snapshots/CUDA_LABEL RESTORED_ENV
```

Live GPU restore requires the same GPU UUID and driver version. GPU capture
without `--filesystem` or the experimental flag is refused. MPS snapshots are
still refused. Failed live graphics attempts publish no usable manifest; they
can leave GPU processes unable to continue, as observed in the native interop
control. For the interactive GPU environments, use filesystem snapshots.

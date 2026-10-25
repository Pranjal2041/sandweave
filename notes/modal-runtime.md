# Modal runtime investigation

Goal: a `modal` runtime in which each Sandweave sandbox is one Modal sandbox,
running directly under Modal's gVisor instead of nesting Sandweave's engine
(see [nested-gvisor-modal.md](nested-gvisor-modal.md), paused).

## Host facts (Modal, 2026-10-25)

Verified on a fresh CPU sandbox and on L40S sandbox `sb-cUFlXNgm3uqY9YzA3t0zOZ`:

- PID 1 is Modal's `dumb-init`; the entrypoint is its child. Root has
  capabilities `0xa80c05fb`, without `CAP_SYS_ADMIN`. A new PID namespace is
  refused unless combined with a new user namespace.
- A user namespace with an identity mapping of all outer IDs works
  (`/proc/self/uid_map` is `0 0 4294967295`). Inside it a new PID and mount
  namespace provide PID 1, procfs, tmpfs and cgroup mounts.
- cgroups are v1 only (`cpu`, `cpuacct`, `cpuset`, `devices`, `job`,
  `memory`, `pids`); cgroup2 is an unknown filesystem type.
- GPU sandboxes inject the NVIDIA user-space driver into standard image paths:
  `libGLX_nvidia`, `libEGL_nvidia`, `libcuda`, the Vulkan ICD at
  `/etc/vulkan/icd.d/nvidia_icd.json` and the GLVND EGL vendor file, with
  `NVIDIA_DRIVER_CAPABILITIES=all`. Device nodes are `/dev/nvidia*`; no `/dev/dri`.

## systemd

`scripts/modal-guest-init.py` makes the template's init PID 1 in private user,
PID and mount namespaces and mounts a `name=systemd` cgroup v1 hierarchy.
systemd then boots (`systemctl is-system-running` reports `degraded`), but
every service that starts a process fails with status 219/CGROUP, including
journald, D-Bus, logind and `tigervncserver@:1`.

Cause, from `scripts/modal-systemd-probe.py --entrypoint shim-cgroup` on a fresh
sandbox: Modal's gVisor accepts the named hierarchy mount, but processes are
not members of it, and a child does not inherit membership. The init can move
itself into a cgroup; its children's writes to `cgroup.procs` fail with `EIO`.
systemd attaches each service's process to its unit cgroup before exec, so no
service can start. The same writes succeed for the `pids` controller hierarchy,
which systemd cannot use as its own.

Consequence: templates whose `init` is `systemd` (GNOME, VR) cannot boot their
normal init on Modal. Command templates do not use systemd.

## Runtime implementation (in progress)

Decision (2026-10-25): on Modal, VR templates run an X server, Monado and the
game directly, without GNOME. Desktop-only templates are rejected.

- `src/sandweave/sandbox/runtimes/modal/`: `configure` (client-side template
  adaptation), `image.py` (Modal image from `install-sandbox-base.sh` and the
  pinned VR inputs on Ubuntu 22.04), `driver.py` (one Modal sandbox per
  Sandweave sandbox; guest agent over a TLS tunnel), `guest_boot.py`
  (entrypoint: driver paths, session bus, Xtigervnc on `:1`), `guest_vr.py`
  (guest-owned ring v2, Monado and input bridge over HTTP) and `vr.py`
  (worker-side stream used by `AttachedVR`).
- The worker must run on Linux with a Sandweave installation and Modal
  credentials. `scripts/modal-runtime-worker.py` creates such a worker on
  Modal's VM runtime with the `sandweave-modal-credentials` Secret.
- Unit tests: `tests/test_modal_runtime.py`.

Live status: two image builds failed and were fixed (the export stub must be
Python; `python3-zstandard` is not packaged for 22.04). The third attempt was
refused by Modal with `Exceeded limit of 3000 for unscheduled Sandboxes`. At
that point new CPU and GPU sandboxes were refused workspace-wide, while this
environment listed only about 70 sandboxes. The guest boot, agent, VR stream
and Open Saber run on `runtime="modal"` are not yet verified.

## Reference: direct VR stack on Modal

The frontier-games A10 environment (`devin-datagen-lawrence-games/.devin/vr-a10`)
started Xvfb, PulseAudio, Monado and Open Saber directly without systemd.
Its Monado metrics recorded 89.9 application FPS over a 30-second window;
that run used Xvfb and VirtualGL, not Sandweave's GNOME/Xvnc desktop.

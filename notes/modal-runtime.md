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

Build history: two image builds failed and were fixed (the export stub must be
Python; `python3-zstandard` is not packaged for 22.04). Creation was then refused
workspace-wide (`Exceeded limit of 3000 for unscheduled Sandboxes`, from other
environments of the shared workspace; this environment held 7 unscheduled) until
06:16 UTC. GPU requests then failed with `Insufficient capacity` after about
22 minutes each, for L40S, A10 and L4 (attempts 6–14). Modal sandboxes accept a
single GPU model, not a list.

### Open Saber acceptance (attempt 15, 2026-10-25)

`scripts/modal-opensaber-check.py` passed on an NVIDIA L40S (driver 610.57.04),
through the public SDK with `runtime="modal"`: guest boot without systemd, agent
over the TLS tunnel, Monado, the game, paired-eye observation, input, a 120-second
recording and Monado metrics. Readiness took 1,203 s, of which 1,177 s was the
runtime phase, mostly waiting for GPU scheduling.

Decoded frames from the actual `left-eye.mp4`/`right-eye.mp4` (60 s) and
`gameplay.mp4` (100 s) show gameplay: both sabers, a directional block, the score
rising from 148 to 240 with combo 1, and the expected eye disparity.

| Measure | Result |
| --- | --- |
| Application FPS (Monado, 120.1 s capture window) | **82.74** (9,938 frames); interval median 11.14 ms, p95 12.65 ms |
| Compositor | 90 Hz target; GPU median 0.21 ms; reused application frames 10.0% |
| Capture cadence (requested 30 FPS) | **1.67 FPS**: 202 paired frames, 0 recorder drops, 10,156 ring frames skipped |
| Input actions (target 120 Hz) | 201 in 120 s, about 1.7 Hz |

For comparison, the nested L40S runs measured 40.6 and 41.5 application FPS with
29.7–29.8 FPS capture. Here the game renders at nearly the 90 Hz target, but the
remote frame and input transport is far below target: each VR call through the
Modal tunnel took about 0.6 s. Capture and input rates are therefore not yet
usable for interactive agents or 30 FPS recordings.

Evidence: `runs/modal-runtime-20261025/modal-opensaber15.tar` (videos, inputs,
metrics, summary, ready record and retry log; lossless frame files excluded),
SHA-256 `c06a07b83e5d012888ddce95a0306912f268e7eb10716dff3cc7605b960c6b32`.

### Relay transport (2026-10-25, worker `sb-sxEmHbbgxbW9p4Mbx43uNn`)

The per-request VR HTTP server was replaced by `guest_io.py`, a guest relay that
owns the fast I/O bridge and the VR ring and serves persistent, authenticated,
length-prefixed connections on one encrypted port. Frames are JPEG-encoded in
the guest and forwarded to the client unchanged. VR input uses its own
connection: an SSL socket must not be read and written from two threads, and
the worker no longer serializes VR observe and input behind its per-sandbox lock.

Open Saber on an L40S, 120 s, through the public SDK (attempt 3):

| Measure | Before (per-request) | Relay |
| --- | --- | --- |
| Capture cadence (requested 30 FPS) | 1.67 | **29.70** (3,571 paired frames, 0 dropped) |
| Input actions (120 Hz loop) | 1.7/s | **108.2/s** (12,984; worker-to-guest RTT median 1.0 ms, p95 1.8 ms) |
| Application FPS | 82.7 | 83.7 (reused frames 8.6%) |

Readiness took 208 s, including 203 s for the runtime phase. Decoded frames from
`left-eye.mp4`/`right-eye.mp4` (60 s) and `gameplay.mp4` (110 s), each 3,571 frames,
show gameplay in both eyes. Transport frames are JPEG (quality 90), so the
recording is lossy; `codec="zstd"` keeps them lossless at a higher byte rate.
Evidence: `runs/modal-runtime-20261025/modal-opensaber-relay3.tar`, SHA-256
`32ff4b1c59afab7518864ac2c89d4b57cb122c305d527b3d25695397f331fa7c`.

## Reference: direct VR stack on Modal

The frontier-games A10 environment (`devin-datagen-lawrence-games/.devin/vr-a10`)
started Xvfb, PulseAudio, Monado and Open Saber directly without systemd.
Its Monado metrics recorded 89.9 application FPS over a 30-second window;
that run used Xvfb and VirtualGL, not Sandweave's GNOME/Xvnc desktop.

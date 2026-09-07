# Environment lifecycle API

The standalone lab exposes a Python API in `scripts/environment.py` and a JSON
CLI in `scripts/env.py`. Existing `run-gvisor.py`, `checkpoint-gvisor.py` and
`verify-snapshot.py` remain available. No Gym Anything contracts have changed.

[Xvnc fast I/O](xvnc-fast-io.md) is available through `manager.fast_io(name)`.
Pause/save detaches its external shared-memory buffer before freezing the guest;
the next I/O request after resume/load reconnects it. Stop also closes its service.

## Operations

| Operation | Behavior |
| --- | --- |
| `start` | Launch a fresh named environment; wait for the runtime to run. Application/service readiness is separate. |
| `pause` | Stop guest tasks in place and retain RAM, writable files, processes, ports and device contexts. Repeating it is harmless. |
| `resume` | Continue a paused environment in place; resume CPU sharing. Repeating it on a running environment is harmless. |
| `save` | Publish a snapshot and leave the source in its previous running/paused state. Verification runs asynchronously. |
| `load` / `restore` | Restore into a fresh name using the saved runtime, resource settings and network policy. Filesystem restores wait for persistent mounts to be reconstructed. |
| `stop` | Save to persistent `snapshots/` before terminating the runtime and its owned helpers. Return the saved snapshot path. Repeating a completed stop is harmless. |
| `stop --discard` | Terminate without saving. Unsaved RAM and writable filesystem changes are lost. |
| `status` / `list` | Return runtime state, process identities, ports, log location and the last stop's snapshot, if present. |

The root overlay and persistent writable mounts occupy guest RAM. Consequently,
an ordinary runtime kill would also discard the writable filesystem. Default
`stop` first pauses, saves and publishes, then deletes the runtime. If saving
fails, it does not delete the environment and returns it to its previous
running/paused state. A successfully published snapshot remains available even
if subsequent teardown fails; `stopped.json` records whether cleanup completed.
Stop is a forced runtime termination after saving, not an application shutdown
or systemd poweroff. Filesystem snapshots retain their crash-consistency limits.

Pause retains allocated host RAM and GPU VRAM. It does not release the Slurm
allocation. The engine pauses guest tasks through its control RPC; host helpers,
external peers and wall time continue. Network peers can time out. Already
submitted GPU work may finish; pause does not partition or freeze the physical
GPU. It does not invoke CUDA checkpointing or discard graphics contexts.

`resume` applies to a resident paused environment. After `stop`, use `load` with
the returned snapshot and a fresh name. Names remain reserved by their bundles;
logs and snapshots are retained. Nothing silently replaces an existing bundle.

## CLI

From `~/scratch/general-vm`:

```bash
python scripts/env.py start demo
python scripts/env.py pause demo
python scripts/env.py status demo
python scripts/env.py resume demo
python scripts/env.py save demo demo-save
python scripts/env.py stop demo --label demo-stopped
python scripts/env.py load snapshots/demo-stopped demo-restored
python scripts/env.py list --active
python scripts/env.py stop demo-restored --discard
```

`start` boots `/sbin/init` by default. Pass normal launcher settings after
`--launch-options`; optionally append `--` and a guest command. For example,
the existing nested Docker bootstrap remains available:

```bash
python scripts/env.py start moodle --launch-options \
  --nftables --guest-gs --docker-data --cgroup v1 \
  --docker-archive images/gvisor-moodle-persisted-docker.tar \
  -- /usr/local/bin/engine-docker init
```

`load` also accepts `--launch-options`, including `--cpus` for an eligible host
CPU pool. A guest command override is accepted only for a filesystem restore.
`--verify` waits for explicit checksum verification before restoring. Startup
timeouts leave the named environment available for inspection or explicit stop;
an early launcher failure reports the log tail rather than returning success.

## Python

With `scripts/` on the Python import path:

```python
from environment import EnvironmentManager

envs = EnvironmentManager()  # optional explicit lab=Path(...)
envs.start("demo", options=["--memory-mib", "4096"], command=["/sbin/init"])
envs.pause("demo")
saved = envs.save("demo", "saved-while-paused")
envs.resume("demo")
stopped = envs.stop("demo")
envs.restore(stopped["last_stop"]["saved"]["snapshot"], "restored")
envs.stop("restored", discard=True)
```

Methods return dictionaries with the same fields as the CLI's JSON. Failures
raise exceptions. `save` and `stop` accept `mode="auto"` (default), `"live"`,
`"filesystem"` or `"experimental-gpu-live"`; `save(local_only=True)` is an
explicitly nondurable diagnostic option. Stop always publishes persistently.

## GPU snapshot selection

| Environment | Default save/stop snapshot | Explicit alternative |
| --- | --- | --- |
| CPU | RAM + processes + writable files and virtual-kernel state | `--filesystem` / `mode="filesystem"` for cold restore |
| GPU | Whole persistent filesystem, followed by fresh processes on restore | Experimental RAM + processes + CUDA only, with explicit opt-in on save and load |
| Experimental MPS | Snapshot rejected; use explicit discard to terminate | MPS snapshot support remains deferred |

GPU live capture uses `--experimental-gpu-live` on `save` or `stop`; load also
requires `--experimental-gpu-live`. Python uses `mode="experimental-gpu-live"`
and `load(..., experimental_gpu_live=True)`. Only a small CUDA control is
validated. Firefox/Earth/Resolve live graphics capture remains unsupported.
The CUDA capture engine temporarily runs guest tasks to execute its CUDA
checkpoint utility even when the source was paused, then reinstates its pause.
This exception belongs to the opt-in experimental snapshot path.

Older running desktops retain their pinned engine. GPU filesystem capture needs
the updated engine, as documented in [GPU snapshots](gpu-filesystem-snapshots.md).
Updating lab scripts does not change the runtime of a resident environment.

## Coordination and teardown

Host-side per-environment file locks serialize pause, resume, stop and save,
including direct `checkpoint-gvisor.py` calls. A conflicting command fails with
an operation-in-progress error. The API passes the same lock descriptor to its
checkpoint subprocess so stop holds ownership through capture and deletion.
Raw `runsc` commands bypass this API's coordination and should not be mixed with
concurrent lifecycle calls.

Pausing releases the CPU broker's throttle and keeps a suspension marker until
resume. Checkpointing preserves an existing marker. The engine's user pause and
CPU throttle are independently nested; broker activity cannot undo a user pause.

The launcher records its PID/start time and its owned runtime/transport children.
Teardown uses `runsc delete --force`, waits for launcher cleanup, and checks the
recorded processes. Shared CPU controllers and external MPS services are not
owned runtime children. Older launchers use a narrow direct-child fallback for
their passt, relay and Apptainer processes. Any final forced signal uses a pidfd
and validates process start time to avoid signaling a reused PID. The current
cluster Python/libc lacks pidfd wrappers, so the Linux syscalls are called
directly on x86_64/aarch64.

## Validation

`python scripts/test-environment.py` exercises lock exclusion/inheritance,
preservation of user-pause markers, failed-save recovery, explicit GPU opt-in,
bundle protection, shared-helper exclusion and PID-safe signaling.

`python scripts/test-lifecycle-live.py` boots disposable quota-controlled peers,
checks that pause actually stops guest progress while the peer continues, saves
while paused, resumes, stops the source, restores its earlier state into a fresh
name and discards a paused clone. It checks RAM identity, deleted-open files,
ownership/modes, queued Unix sockets, internal TCP, controller survival and
transport cleanup. It cleans up its environments and retains snapshots/logs.
Generated results are in `runs/lifecycle-acceptance.json`.

`python scripts/test-lifecycle-gpu-live.py --gpu 0` creates disposable GPU
environments, cycles pause/resume with live CUDA and CUDA/OpenGL resources,
checks VNC, stops to a filesystem snapshot and cold-loads it. It also injects
an unsupported writable mount to verify that a failed stop-save leaves the
source running, then separately saves/stops/restores the small CUDA-only
control through the experimental API. It cleans up its environments.

Both suites passed on `babel-u5-28`, 2026-09-07. The
[CPU result](lifecycle-evidence/cpu.json) records pause 0.234 s, resume 0.129 s,
stop including save 0.809 s, and load after source teardown 0.559 s for the small
probe. The [GPU result](lifecycle-evidence/gpu.json) records three pause/resume
cycles at 0.242–0.282 s / 0.168–0.192 s, with the same CUDA allocation and
CUDA/OpenGL process/buffer contents. These are small control workloads, not
latency guarantees for a large desktop. GPU graphics checkpoint support is
unchanged; these graphics checks resumed resident contexts.

The nine lifecycle unit tests, five filesystem tests, fifteen snapshot-store
tests and three CPU-broker tests passed. CLI acceptance additionally exercised
a custom guest command, pause/resume, local save, restore and explicit discard.
The [CLI result](lifecycle-evidence/cli.json) and completed background
[snapshot verifications](lifecycle-evidence/verification.json) are recorded.
The two pre-existing Resolve desktops were preserved throughout. The engine
remains at `59487a0`; this change adds host-side orchestration only.

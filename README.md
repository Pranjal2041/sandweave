# General VM: no-KVM Slurm lab

Current runtime: patched **gVisor systrap**, running in unprivileged Apptainer with no KVM access, host sudo, or administrator changes. This independent lab retains a Linux desktop, guest root, systemd, and actual nested Docker. Gym Anything's runtime code remains untouched.

The [feature inventory](notes/feature-inventory.md) lists execution modes,
lifecycle and snapshots, resource/network controls, desktops, automation,
GPU/VR, recording and verified application workflows, with experimental limits.

The [repository architecture proposal](notes/repository-architecture.md) maps the
agreed API to six pillars, their atomic units and adapter contracts, with a
package layout and an extraction plan for the existing lab. It is design-only;
the public package has not been scaffolded or implemented.

## Agreed public API contract (v1)

**Agreed on 2026-09-08; SDK and CLI not implemented yet.** `sandweave`
(Sandweave) is the working package/CLI name. This section is the source of truth
for implementation and the future public repository. Preserve these examples,
names, defaults, return semantics and lifecycle behavior. Any public contract
change requires an explicit agreed revision; do not silently substitute a
different interface during implementation. The [detailed contract](notes/sandbox-api-proposal.md)
and [extended examples](notes/sandbox-api-examples.md) must stay consistent with
this section. Existing lab scripts below remain the implemented interface.

### A coding sandbox

```python
from sandweave import Sandbox

with Sandbox() as env:
    result = env.run("python -c 'print(2 + 2)'")
    print(result.stdout)
```

The default is a small coding environment with Python, shell and basic tools.
`Sandbox(...)` returns only when the environment and its declared capabilities
are ready. No desktop, GPU, cloud account, host sudo or KVM is required for this
coding example. Templates, setup scripts and hardware remain configurable.

**`run` and `exec` take one command string.** The default is `/bin/sh -c` inside
the sandbox, with shell quoting, variable expansion, pipes, redirection and `&&`.
No SDK-host shell runs the command. Templates may declare `command_shell`, and
`shell="/bin/bash"` selects a different guest shell for a call; execution is
noninteractive and non-login. Each call starts a new process/shell, so use `cwd`
and `env` arguments for per-call state. The result uses the selected shell's exit
status, with no implicit `errexit` or `pipefail`.

`run` waits and returns `CommandResult` with `stdout`, `stderr` and `returncode`.
Nonzero exits raise unless `check=False`; execution timeouts remain typed errors.
`exec` returns a `Process` with stdin/stdout/stderr, `wait`, `poll` and `terminate`:

```python
from sandweave import Sandbox

with Sandbox() as env:
    env.files.upload("./train.py", "/workspace/train.py")
    process = env.exec("python -u /workspace/train.py", timeout=60)
    for line in process.stdout:
        print(line, end="")
    process.wait(check=True)
```

This example uses a user-provided `train.py`.
Advanced direct execution is available as `env.run(argv=["python", "main.py"])`
or `env.exec(argv=[...])`, with literal arguments and no shell. Exactly one
command string or nonempty `argv` is required; `shell` cannot accompany `argv`.
Variadic positional command arguments are not part of v1.

### A custom desktop in three lines

```python
from sandweave import Sandbox
env = Sandbox(template="gnome")
env.setup("./install-chrome-and-myapp.sh")
```

The user-written setup script runs inside the guest. The template automatically
supplies the desktop and interaction capability:

```python
image = env.desktop.screenshot()
env.desktop.mouse.click(400, 300)
env.desktop.keyboard.type("hello")
```

Users choose the installed applications and preparation scripts. A custom
template declares services that must start on every fresh boot. Chrome here is
an illustrative installation choice, not a newly verified application.

### Cache once, restore by name

```python
baseline = env.cache("my-workbench")
env.terminate()

with Sandbox(cache="my-workbench") as env:
    image = env.desktop.screenshot()
```

A cache includes the resolved template, service startup and capability metadata,
so restoring it does not need the template again. `env.cache()` defaults to
filesystem state: installed software/files survive, while processes start fresh.
The returned immutable `baseline` reference can replace the name to pin an exact
revision. Each restore receives independent writable state; explicitly shared
external volumes have their own policies.

For automatic reuse of preparation:

```python
with Sandbox(template="gnome", setup="./install-tools.sh",
             cache_key="tools-build") as env:
    image = env.desktop.screenshot()
```

`cache_key` reuses matching preparation or prepares a new revision when the
template, script or declared inputs change. `cache` restores saved state and
raises on a miss. A setup script alone uses the coding template; a template may
also be a local file or object:

```python
with Sandbox(setup="./setup-coding.sh") as env:
    print(env.run("python --version").stdout)

with Sandbox(template="./my-desktop.toml") as env:
    image = env.desktop.screenshot()
```

### Many independent coding environments

```python
from sandweave import Pool

def evaluate(env, source):
    env.files.write_text("/workspace/main.py", source)
    return env.run("python /workspace/main.py", timeout=5, check=False)

programs = ["print(1 + 1)", "print(2 + 2)"]

with Pool(template="coding", size=32, warm=8) as pool:
    results = list(pool.map(evaluate, programs))
```

`evaluate` runs in the caller's Python process with a leased sandbox. `size`
bounds concurrent leases; `warm` requests an idle-ready reserve within that
capacity and actual resources. Pool entry waits for the initial reserve.
Every task receives independent starting state. Used sandboxes are disposed of
and replaced from an immutable baseline; deleting a workspace directory does
not establish a clean episode. Pools also accept `cache=baseline` and pin its
revision. Results preserve input order by default, and outstanding work is bounded.

### Async uses the same contract

```python
from sandweave import Sandbox

async def evaluate_one(source):
    async with await Sandbox.create.aio(template="coding") as env:
        await env.files.write_text.aio("/workspace/main.py", source)
        result = await env.run.aio("python /workspace/main.py", timeout=5)
        return result.stdout
```

Blocking I/O methods have Modal-style `.aio` counterparts. `run` completes a
command; `exec` exposes a process. Neither requires an RL framework.

### VR games and agent loops always observe both eyes

```python
from sandweave import Sandbox

def play_episode(policy):
    with Sandbox(template="vr/gunspinning", gpu=True) as env:
        observation = env.vr.observe()
        with env.vr.record("./episode", fps=30):
            for _ in range(300):
                action = policy(observation.left, observation.right)
                if action is None:
                    break
                observation = env.vr.step(action)
```

The template supplies game startup, Monado/xrizer, virtual controllers and
stereo I/O. Observations contain actual left/right images from the same
compositor frame. Recording preserves lossless pairs and finalizes both eye
videos, a synchronized side-by-side preview and timing/drop metadata.
`vr.step` captures after runtime acknowledgement; it does not imply game-level
input consumption or exactly one simulation tick. GPU filesystem caches boot
fresh game processes; they do not promise live graphics restoration.

Desktop agents use `env.desktop.step(action)` and receive an observation with
`.image` and timing/acknowledgement metadata. Capturing after an input-server
fence does not promise application repaint completion. Plugins may add
`env.capability("robotics")` with their own action/observation schemas and
simulator stepping semantics. Policy, reward and training-stack choices remain
downstream. [Extended examples](notes/sandbox-api-examples.md) include desktop
and robotics loops, file transfer, explicit allocation and checkpoint restoration.

### Resources and placement stay explicit

```python
from sandweave import CPU, Memory, Sandbox

with Sandbox(template="cuda", gpu="L40S",
             cpu=CPU(vcpus=2, weight=200, quota=1.5),
             memory=Memory(guest="4GiB", runtime="1GiB")) as env:
    print(env.run("nvidia-smi").stdout)
```

Scalar conveniences such as `cpu=2`, `memory="4GiB"` and `gpu=True` remain
available. CPU weights apply within a worker's shared CPU pool; runtime/helper
memory is additional to the guest budget. GPU selection uses eligible allocated
devices. New Slurm allocation is an explicit `Slurm.acquire(...)` operation;
closing a sandbox must never cancel an unrelated or borrowed allocation.

gVisor is the default runtime, with `runtime="apptainer"` an explicit alternative
that reports its actual capabilities. Runtime choice is separate from local/SSH
placement and multi-worker pools. These paths require neither KVM nor host sudo;
host syscall/user-namespace restrictions and device compatibility still matter.

### State, ownership and performance

| Operation or source | Locked meaning |
| --- | --- |
| Template/setup | Inspectable preparation, startup, readiness and capabilities. |
| Filesystem cache | Reusable software/files; fresh processes on restore. |
| `env.snapshot(state="memory")` | Supported running process/kernel state; unsupported GPU graphics capture fails explicitly. |
| `env.pause()` / `env.resume()` | Suspend/continue the same resident environment, retaining memory/VRAM. |
| `env.stop()` | Save before releasing; save failure keeps the source alive. Return a checkpoint reference. |
| `env.terminate()` | Release without a new save; preserve previously published caches and external volumes. |
| `env.close()` | Disconnect this client only. |
| `with Sandbox(...)` | Owned ephemeral scope; discard unsaved state on exit. Save/cache or successfully stop first to retain state. |
| `with Sandbox.connect(id)` | Borrowed handle; exit only disconnects. |
| Prestarted pool | Ready independent environments; checkout and refill are separate work. |

Optimize cold startup, prepared restore, actions and execution. A few
milliseconds for local warm checkout plus the first small command is a target,
not a current measurement or promise for cold desktop/Slurm startup. Measure
usable readiness and first work, including queue time, at p50/p95/p99. Use
persistent control/data paths and clean baselines; do not hide startup work
behind early handle creation or sacrifice episode isolation for reuse.
Measure guest-shell launch cost in the default command-string path as well as
the explicit direct-execution path.

The internal boundaries remain template resolution, runtime operations, target
and allocation handling, artifact storage, capability plugins and pooling.
Built-in desktop/VR capabilities must use the public extension mechanism.

### CLI parity

```bash
sandweave run --template coding -- "python -c 'print(2 + 2)'"

sandweave create --template gnome --setup ./install-tools.sh --name workbench
sandweave desktop screenshot workbench --output screen.png
sandweave cache save workbench my-workbench
sandweave create --cache my-workbench --name restored

sandweave create --template vr/gunspinning --gpu auto --name gunspin
sandweave vr record gunspin --duration 30 --output ./episode
```

`run` creates an ephemeral sandbox for one command; `create` returns an explicitly
managed environment. `run`/`exec` take exactly one quoted command string after
`--` for the guest shell. Advanced `--argv -- PROGRAM ARG ...` bypasses it. The
CLI never joins separate arguments into a shell command. Process stdout/stderr
and exit codes pass through; control diagnostics go to stderr. Python and CLI
share lifecycle and capability semantics.

## Current lab implementation and evidence

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
checks. [GunSpinning VR now runs in both native gamepad and tracked-controller
modes](notes/gunspinning-vr-experiment.md), through a userspace joystick proxy
or xrizer/Monado. Offline launch, aiming, firing and reloading were exercised;
the verified VR gameplay sample reached 61 FPS on L40S with Xvnc presentation.
Gamepad analog triggers, audio, physical headset delivery and full-game
completion remain unvalidated.
The [GunSpinning stereo recording](notes/gunspinning-vr-experiment.md#both-eye-video-demo)
includes separate left/right eye videos and a synchronized side-by-side preview.
The [broader Linux VR catalogue](notes/linux-vr-catalog-and-build-options.md)
separates native games, community VR ports, reported Proton gameplay, and
original games we could build for training. It records source, store, input,
and compatibility limitations; Windows VM work is paused.
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

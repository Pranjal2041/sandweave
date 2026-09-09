# Agreed sandbox API contract: Sandweave

Status: **agreed contract v1, frozen on 2026-09-08; not an implemented SDK**.
The user approved the design with single command strings as the default for
`run`/`exec`. The [README contract](../README.md#agreed-public-api-contract-v1)
is the source of truth for implementation and the future public repository;
this document specifies its detailed semantics. Changes to the documented
interface or behavior require an explicit agreed contract revision, not silent
implementation drift. `sandweave` remains the working package/CLI name; no
package or domain is being registered. No runtime changes or environment
launches accompany this documentation.

See [downstream examples](sandbox-api-examples.md) and the existing
[feature inventory](feature-inventory.md). Agreed method names in this document
must not be mistaken for methods already available in `scripts/environment.py`.

## 1. Design commitments

| User priority | API consequence |
| --- | --- |
| Simple Python and CLI | One `Sandbox` handle; ordinary calls work without an App, deployment decorator, mandatory server account or RL framework. The constructor returns a usable environment. |
| Customizable with easy templates | A template supplies a base, setup, service startup, readiness and interaction capabilities. Users can supply their own scripts, files and templates and override defaults. |
| Modular and extensible | Separate runtime, execution target, artifact store and interaction adapters. Add a robotics or desktop adapter without changing process execution or lifecycle semantics. |
| Fast startup, actions and execution | Minimal coding template; prepared caches; persistent control/data channels; optional prestarted pools. Measure readiness and first useful work, not just handle allocation. |
| Flexible hardware and placement | CPU sharing and budgets remain explicit. Local/current-allocation operation is the default; SSH and Slurm are target adapters. Official execution paths require neither KVM nor host sudo. |
| Useful for training and evaluation | Independent episodes, stable observation/action schemas, async operation, bounded queues, timestamps and explicit failure outcomes. Rewards, policies and training libraries remain downstream choices. |

Templates are presets, not a mandatory application taxonomy. GNOME is not part
of a coding sandbox; Xvnc is not the isolation engine; Monado is not a desktop
backend. CPU/GPU, display, XR and placement remain separately configurable.

## 2. Research that informs the contract

| Primary source | Relevant finding and design choice |
| --- | --- |
| [Modal Sandbox reference](https://modal.com/docs/sdk/py/latest/Sandbox) | A sandbox is a handle with creation parameters, process execution and resource configuration. Borrow that shape; omit a mandatory Modal-style App object for local use. |
| [Modal command execution](https://modal.com/docs/guide/sandbox-spawn) | `exec` returns a process with streams and waiting, including `.aio` methods. Keep this process contract and add a blocking `run` convenience. Our command-string default is a deliberate interface choice; it does not copy Modal's positional argument convention. |
| [Modal readiness](https://modal.com/docs/guide/sandboxes#readiness-probes) | Container startup and service readiness differ. Our convenient constructor waits for the template's declared readiness; this is a deliberate difference from returning an asynchronously provisioning handle. |
| [Modal images and caching](https://modal.com/docs/guide/images#image-caching-and-rebuilds) | Image definitions drive cache reuse and rebuilds. Use recipe fingerprints and explicit refresh; do not trust a human cache label alone. |
| [Modal sandbox snapshots](https://modal.com/docs/guide/sandbox-snapshots) | Filesystem, directory and memory snapshots have distinct meanings and limits. Keep filesystem caches and process checkpoints distinguishable. We do not adopt Modal's retention periods or its current memory-snapshot limitations as our own. |
| [Modal VM sandboxes](https://modal.com/docs/guide/vm-sandboxes) | A full VM runtime is a separate option. Borrowing Modal's interface does not mean adopting a VM backend or adding a KVM requirement. |
| [E2B template quickstart](https://docs.e2b.dev/template/quickstart) | Templates include build configuration and startup/readiness behavior. Retain that useful distinction while accepting a plain setup script. |
| [Daytona warm pools](https://www.daytona.io/docs/en/warm-pools/) | Precreated running sandboxes can satisfy matching creation requests. Design an explicit pool whose compatibility key includes the actual environment configuration. This source is architectural precedent, not evidence of our latency. |

These findings were checked against the current official documentation. The
following choices are our agreed design, not claims that those vendors expose this
exact API. Their performance claims are not transferred to this lab.

## 3. Small public surface

```python
from sandweave import Sandbox

env = Sandbox(template="gnome")
env.setup("./install-tools.sh")
```

`Sandbox(...)` is a synchronous convenience for `Sandbox.create(...)`. Async
creation is `await Sandbox.create.aio(...)`; I/O methods have the same Modal-style
`.aio` counterpart. No synchronous constructor is called inside an async example.

| Operation | Contract |
| --- | --- |
| `Sandbox(template=..., setup=...)` | Create from a built-in template name, a local template file/object, and/or a local setup script. With only `setup`, use the coding template. |
| `Sandbox(cache=...)` | Create an independent environment from a named cache or immutable cache reference, including saved template/capability metadata. A miss raises `CacheMiss`. |
| `Sandbox(snapshot=...)` | Restore a particular checkpoint into a new environment; enforce its compatibility constraints. |
| `Sandbox.connect(id)` | Attach to an existing environment; never creates a replacement or implicitly resumes a paused one. |
| `env.run(command, ...)` | Run one command string through the guest shell and wait; return `CommandResult(stdout, stderr, returncode, ...)`. Raise on nonzero exit by default; `check=False` supports test/evaluation outcomes. |
| `env.exec(command, ...)` | Run one command string through the guest shell; return a `Process` immediately after process creation, with stdin/stdout/stderr, `wait`, `poll` and `terminate`. |
| `env.run(argv=[...], ...)` / `env.exec(argv=[...], ...)` | Explicit advanced direct-process execution with literal arguments and no shell. Mutually exclusive with a command string. |
| `env.setup(path, ...)` | Upload an explicit local script/context and execute it inside the sandbox. Wait for success; never execute it on the SDK host. |
| `env.files` | File read/write, upload/download and streaming access. |
| `env.cache(key, state="filesystem")` | Capture and publish a reusable immutable environment revision under a human-readable name; return a `SnapshotRef`. Leave the source alive. |
| `env.snapshot(state="memory")` | Capture a checkpoint and return a `SnapshotRef`; unsupported state kinds fail explicitly. |
| `env.pause()` / `env.resume()` | Suspend/continue the same resident environment. |
| `env.stop(state="auto")` | Preserve state, then release the runtime; return the checkpoint reference. A failed save does not terminate the source. |
| `env.terminate()` | Explicitly release the runtime without a new checkpoint. Does not delete already published caches, volumes or logs. |
| `env.close()` | Disconnect this client only; do not stop the sandbox. |
| `env.desktop` / `env.vr` | Typed interaction namespaces supplied by the template's capabilities. Missing capabilities raise a descriptive error. |
| `env.capability("robotics")` | Bind a versioned third-party capability with its own typed API/schema. |
| `env.spec`, `env.capabilities`, `env.status()`, `env.timings` | Inspect resolved configuration, available operations, current status and measured startup stages. |

`template` and `setup` compose. `cache` and `snapshot` are alternative sources and
cannot be combined with a new recipe; modify a restored environment explicitly
with `setup`. Optional `cache_key` on a recipe enables preparation reuse, as
specified below. Source selection is keyword-based; strings are not guessed to
be shell commands, existing sandbox IDs or templates interchangeably.

### Defaults and overrides

- Default template: a small coding environment with Python, shell and basic
  tools, no desktop, no GPU and no systemd requirement. Initial proposed preset:
  one advertised CPU, 1 GiB guest-page budget and a separate 512 MiB runtime
  guard. These are tunable defaults to qualify, not current startup results.
  A minimal supervisor keeps it available and reaps command children; one
  command exiting does not implicitly end the whole sandbox.
- Default runtime: gVisor. Apptainer-only execution is an explicit alternative,
  with its different capability/enforcement report. Never silently switch to it
  because gVisor lacks an operation or device feature.
- Default target: this Linux host/current Slurm allocation. Do not submit new
  jobs or spend resources in another provider implicitly. An SDK on a laptop
  can use an explicitly configured Linux SSH target.
- Default network: the existing public-IPv4 policy; offline is one option.
  Setup obeys the requested network policy too. An offline cache miss does not
  authorize an online build behind the user's back.
- Template-specific resources, execution user, services and display settings
  are defaults. Explicit constructor values override them. Required hardware
  and unsupported operations are validated, not faked by silent downgrades.
- `setup` uses the template's preparation user (guest root for the proposed
  built-ins); `run`/`exec` use its application user, matching the desktop session
  when present. Both accept an explicit user override. The coding template
  provides a writable Python environment for that application user.
- No automatic lifecycle timeout in the core SDK; a user/template/provider can
  set one. The enclosing allocation's expiry remains a real external limit.
- `env.spec` reports the resolved, immutable launch specification, including
  defaults. Configuration precedence is core defaults, template inheritance,
  explicit launch overrides, then validation against target constraints.

Optional advanced arguments include `cpu`, `memory`, `gpu`, `network`, `env`,
`mounts`, `target`, `runtime`, `name`, `ttl`, `startup_timeout`, `keep_on_error`,
and typed runtime/provider options. Do not forward arbitrary host shell flags
through the common API. Extension-specific settings are namespaced and validated.

## 4. Templates are inspectable recipes with capability metadata

A versioned template consists of:

1. Base filesystem/image identity and required architecture/runtime features.
2. Preparation scripts and explicit input files, with their digests.
3. Service entrypoints and readiness probes.
4. Resource and network defaults.
5. Interaction adapters and their action/observation schema versions.
6. Cold-start, snapshot-detach and restore-attach hooks where required.

Built-in examples: `coding`, `gnome`, `docker`, `cuda`, `vr/opensaber`,
`vr/gunspinning`. These are proposed packaged templates around the lab's accepted
workflows, not template files already shipped today. Third-party templates use
the same public mechanism. A bare image or imported OCI image can be described
by a `Template` object; image conversion is a preparation step, not a promise
that every registry image works unchanged with every runtime.

Local TOML is the initial declarative format. Paths are relative to that file.
Inheritance is explicit, pinned when resolved, and rejects cycles. Nested
configuration merges by field; service/capability entries merge by name;
sequences replace rather than silently append. An entry can be disabled
explicitly. Constructor `setup` appends a preparation stage after the template's
own setup; changing the parent stage requires an explicit template override.

```toml
schema_version = 1
extends = "gnome@1"

[setup]
script = "./install-tools.sh"
inputs = ["./requirements.lock", "./vendor-packages/"]
user = "root"

[resources]
cpu = 4
memory = "8GiB"

[capabilities.desktop]
resolution = [1280, 800]

[services.my_app]
command = ["/opt/my-app/bin/start"]
ready = { exec = ["/opt/my-app/bin/healthcheck"] }
```

Setup scripts honor their declared interpreter and exit status. Scripts and
declared inputs are copied explicitly; the host home/repository is not mounted
implicitly. A script that reads local auxiliary files needs a declared context.
Package managers, languages, install locations and application startup scripts
remain user choices. A template's prepare phase is reusable; its start phase
runs on a fresh cold boot. Live restore resumes saved processes and invokes only
reattachment hooks, avoiding duplicate service launches.

## 5. Caches: convenient names, precise state

### Explicit capture

```python
env.cache("chrome-tools-v1")
```

Default `state="filesystem"` captures persistent root/mount data, not process
RAM. It also saves the resolved template, service definitions, plugin versions
and compatibility manifest. Thus `Sandbox(cache="chrome-tools-v1")` knows how
to boot the desktop and expose its actions without a separate template argument.
The cache does not merely contain a directory of application files.

`state="memory"` additionally captures running processes and virtual-kernel
state on supported CPU sandboxes. GPU memory capture is not silently treated as
a filesystem cache: unsupported graphics capture raises an error. The existing
small CUDA-only checkpoint path remains an explicitly experimental backend
option, not a general GPU promise.

Every capture returns an immutable `SnapshotRef` with ID/digest, state kind,
source/template identity, dependency manifest, verification status and storage
location. Human names point to revisions; moving a name does not mutate old
revisions or already running environments. `Sandbox(cache=ref)` pins an exact
revision for reproducible training. Name publication uses an atomic compare-and-
swap; competing writers cannot unknowingly replace each other's revisions.

### Automatic preparation reuse

```python
env = Sandbox(template="gnome", setup="./install-tools.sh",
              cache_key="chrome-tools-build")
```

- Fingerprint the resolved template/base, script contents, declared context,
  preparation environment and relevant plugin/toolchain versions.
- A matching completed preparation restores without repeating setup. A missing
  or changed recipe builds a new revision. Concurrent identical builds share one
  preparation operation; waiting callers have their own deadlines.
- Publish only after successful preparation and required readiness checks, and
  before handing the mutable environment to user code. Partial/failed builds
  never become cache hits.
- A manually captured environment is not automatically considered equivalent
  to a recipe build. Captured and prepared provenance are recorded; a name used
  for an incompatible cache purpose produces a clear conflict rather than a
  false hit. Use different labels for manual checkpoints and build caches.
- `refresh=True` forces a new preparation revision. External package repositories
  changing without a recipe/input change are not automatically detectable; use
  lockfiles/pinned assets or refresh. A cache is not proof of a hermetic build.

`cache=` means **restore this saved state**; `cache_key=` means **reuse preparing
this recipe**. A cache-only miss fails, because guessing another environment
would violate the user's intended starting state.

### Storage and consistency

Caches are immutable baselines; each created sandbox gets independent writable
state. Explicit external writable volumes remain separate resources and are not
silently included, rolled back or cloned with the root filesystem. The manifest
lists required mounts and their snapshot behavior. An unhandled writable mount
causes capture to fail, matching the current filesystem-snapshot discipline.

An optional `env.files.snapshot(path)` produces a directory artifact for an
explicit mount; it is not a bootable environment cache. This directory/volume
SDK is new work. Default access to shared dataset mounts is read-only; writable
sharing requires explicit configuration and inherits the storage backend's
consistency semantics.

Publishing returns only after complete payloads/dependencies are available in
the configured persistent store. Full integrity hashing may continue in the
background as today; the reference exposes `verification`, and `ref.verify()`
waits for explicit validation. Known failures block restore. A node-local-only
artifact must be explicitly requested and labeled nondurable. Cache expiry and
pruning are configurable; named caches have no hidden short TTL by default.
An integrity failure discovered later is reported to existing users of that
revision; it does not silently terminate running environments.

Filesystem snapshots remain crash-consistent unless an application hook offers
a stronger cut. Secret mount contents are excluded/rebound by their declared
policy; bytes an application copied into ordinary files or process RAM may be
captured. Do not promise automatic credential scrubbing. External services and
side effects do not roll back.

## 6. Readiness, process execution and lifecycle

### Ready means usable

Successful normal creation means: resources resolved, runtime running, mounts
restored, setup succeeded, template services ready and advertised capabilities
attached. A coding template waits for command execution; GNOME additionally
checks its desktop/input connection; VR checks its runtime/app submission and
paired-eye observation path. A game menu may be the declared initial state;
level-specific readiness belongs to that game template.

`Sandbox.create.aio` has the same readiness guarantee. An operation ID and stage
events allow monitoring during a slow create. Returning a handle early is not
reported as successful startup. On failure, retain diagnostic logs and report
phase, cause and operation/sandbox IDs. Default cleanup concerns only resources
owned by that attempt; `keep_on_error=True` retains a failed sandbox for debugging.
An unknown remote outcome is reconciled, not declared cleaned up speculatively.

### Commands and failures

`run` and `exec` take **one command string** by default:
`env.run("python -c 'print(2 + 2)'")`. The string is sent unchanged to a shell
inside the sandbox. Core default: `/bin/sh -c`, noninteractive and non-login.
Quoting, variables, globbing, pipes, redirection and `&&` have that shell's
semantics. No SDK-host shell expansion or whitespace splitting/rejoining occurs.
The template may declare `command_shell`; per-call `shell="/bin/bash"` overrides
it, still using `-c` without login startup files. The selected shell must exist
inside the guest; missing shells fail explicitly. Exit status is the shell's
status; the SDK does not implicitly add `errexit` or `pipefail`.

`env.run(argv=["python", "main.py"])` and `env.exec(argv=[...])` are the explicit
advanced form for literal arguments/direct execution, avoiding shell parsing
and startup. Exactly one of `command` or a nonempty `argv` is required;
`shell` is invalid with `argv`. Variadic positional command arguments are not
part of v1. String and `argv` forms have identical result, timeout, streaming and
async semantics. Use `argv` for programmatically supplied literal argument data.

Optional `cwd`, `env`, `user` and `timeout` apply inside the guest. Repeated
commands start new processes/shells, not a persistent Python interpreter or
shell session; a `cd` or shell variable in one call does not affect another.
`exec`/PTY or a domain adapter can supply sessions. Execution timeouts apply to
the owned shell and its process group, including pipelines.

`exec` provides text streams by default and an explicit binary mode. `run`
drains both output streams concurrently and uses bounded spooling; output over
a configured inline limit has artifact references/truncation metadata. Large
output must not silently consume unlimited SDK RAM or deadlock on stderr.
`Process.wait()` returns the exit code; it raises for nonzero status only when
asked to check. `CommandResult` records timings and whether output was truncated.

Active exec streams require their own snapshot qualification: use guest-owned
PTY/log storage with reattachable client streams, or reject capture while an
attachment is non-restorable. Closing a process's pipes or killing it silently
to make a snapshot succeed would violate the contract. This integration is new
work, not established merely by existing CPU checkpoint acceptance.

Execution `timeout` bounds the command/process group. `wait(timeout=...)` only
bounds waiting and does not kill it. A confirmed execution timeout stops the
owned group and raises `CommandTimeout` with partial output; if the transport
cannot establish the outcome, report `OperationUnknown` and the operation ID.
Async cancellation requests cancellation of owned work; it is not proof that
remote processes are already gone. Mutating commands/actions are not blindly
replayed after a lost reply. Request IDs support deduplication/status lookup;
there is no blanket exactly-once guarantee across failures.

Typed failures include source/setup failure, cache miss/conflict/incompatibility,
unsupported capability, unavailable resources, command failure/timeout,
snapshot integrity failure, and allocation/device loss. Distinguish a test's
nonzero return code from an infrastructure failure. No automatic workload retry
or replay is imposed on the RL application.

### Ownership and teardown

| Usage | Lifetime |
| --- | --- |
| `env = Sandbox(...)` | Explicitly managed lifetime; `close` disconnects, `stop` saves then releases, `terminate` releases without saving. Garbage collection is not lifecycle control. |
| `with Sandbox(...) as env:` | Explicit ephemeral scope: on exit terminate this newly owned sandbox, including on an exception. Publish a cache/checkpoint or call `stop` first to retain its state. A prior successful `stop` makes scope cleanup a no-op. |
| `with Sandbox.connect(id) as env:` | Borrowed handle: exit closes this client's connection, leaving the existing sandbox alive. |
| A pool lease | Ephemeral independent episode; leaving the lease disposes its used sandbox after owned actions/recorders close. |

The ephemeral-context behavior is an agreed convenience and is deliberately
documented; it does not change the existing manager's save-before-stop default.
Snapshot/pause hooks detach non-restorable I/O and release held inputs in order.
A live resume restores the same process identity; `stop` followed by
`Sandbox(snapshot=ref)` creates a new environment. Pause retains memory/VRAM and
does not make external time, peers or already submitted GPU work stop.

## 7. Desktop and VR capabilities

Desktop supplies `screenshot`, `action`, `step`, `mouse` and `keyboard` helpers.
The existing mouse/keyboard dictionaries remain an accepted action schema.
Convenience calls compile to that schema. `step(action)` returns a
`DesktopObservation` with `.image`, frame/action IDs and timing/acknowledgement
metadata. Its guarantee is a capture after the input server fence, not that the
application has repainted. `screenshot()` returns the owned RGB image directly.

VR supplies `observe`, `action`, `step`, `record`, `action_spec` and
`observation_spec`. A `StereoObservation` always has `.left` and `.right`, owned
RGB uint8 H×W×3 pixels from the same compositor frame, plus IDs/timestamps and
drop/reuse metadata. Eye separation/FOV provenance belongs in the schema and
recording manifest; until per-frame telemetry exists, label configured values
as configuration-derived. Never manufacture a second eye from a mono mirror.

`vr.step(action)` submits a validated head/controller state and observes a pair
whose capture follows the runtime acknowledgement. Implementing that barrier
needs more than reading the next already-published ring entry. The result still
does not prove the game consumed the action: its application-applied status is
unknown unless the application supplies an acknowledgement. Games run in wall
time; `step` does not advance exactly one simulation tick. A robotics simulator
can offer true lockstep through a separate capability that advertises it.

VR action fields retain the existing validated pose/control representation:
positions in tracking-space metres, orientation x/y/z/w quaternions and explicit
analog/button fields. `observe()` defaults to the newest complete pair. A
single controller-writer lease prevents competing clients from accidentally
interleaving poses; multiple observers are permitted. Templates provide an
initial neutral state and closing an input owner releases held controls.

`vr.record(path, fps=30)` is optional during training and mandatory for our demo
acceptance workflow. By default it preserves lossless pairs and finalizes left,
right and synchronized side-by-side videos with capture timing. Closing the
recording waits for finalization and reports storage/encoding errors. Bounded
recording queues report dropped pairs explicitly; observation and application
FPS stay distinct. No physical headset, VR audio or exact-tick guarantee is
implied by this API.

## 8. Hardware and placement without hidden requirements

Scalar conveniences expand into typed options:

| Option | Meaning |
| --- | --- |
| `cpu=2` | Two advertised guest CPUs/guest execution parallelism, with default shared-pool weight. It is not an exclusive two-core reservation or aggregate host CPU cap. |
| `CPU(vcpus=2, weight=200, quota=1.5)` | Explicit existing sharing controls. Relative weights apply within a worker's eligible CPU pool, not globally across nodes. |
| `memory="2GiB"` | Guest-page budget; the separately reported runtime/helper overhead remains additional. `Memory(guest=..., runtime=...)` exposes both knobs. |
| `gpu=True` / `gpu="L40S"` | Select one eligible allocated GPU, optionally constrain its model; validate template graphics/driver requirements. No GPU or unsupported hardware gives an explicit failure. |
| `GPU(model="L40S", sm_chunks=2, client_memory="1GiB", experimental=True)` | Opt into the existing cooperative MPS controls and their limits. |
| `network="offline"` | Existing egress-blocking mode, retaining explicitly available incoming control/forwarding. `Network(...)` supplies additional CIDRs. |
| `target="local"` | Current host/current Slurm allocation, respecting its CPU/device limits. |
| `target="ssh://worker"` or a configured target name | Run on an explicitly selected reachable Linux worker. SDK and sandbox need not share a machine. |

`targets=[...]` on a pool combines eligible workers/allocations; each sandbox
still lives on one worker. The scheduler chooses placement and admission based
on capability and capacity, then the local controller handles sharing. It must
report requested/effective resources and any enforcement gaps. Remote CPU
counts are not added together into one fictitious larger machine.

An explicit `Slurm.acquire(...)` operation obtains a new allocation; its result
is usable as `target=`. Queue wait and worker boot are outside the cached sandbox
fast path. Admission is not an implicit GPU request. Existing-job targets never
own or cancel the user's whole job when a sandbox closes. An allocation created
by an explicit allocation context is owned by that context, which drains its
own sandboxes before release. Preemption produces an event/typed failure;
automatic checkpointing and resubmission need separate opt-in implementations.

The target capability report includes unprivileged execution support, runtime
versions, CPU architecture/features, allocated devices, driver compatibility,
memory limits and reachable stores. Checks may be cached for a worker epoch;
device loss/allocation changes invalidate them. Missing KVM is normal. Some
hosts still restrict user namespaces, syscalls or GPU access, so no API can
promise identical workloads on arbitrary hardware without checking those facts.

## 9. Performance contract and pools

| Creation path | Work that remains |
| --- | --- |
| Cold recipe | Resolve/fetch base and inputs, prepare, start runtime/services and attach capabilities. |
| Filesystem cache hit | Avoid installation; still allocate/start the runtime and start services. |
| Memory checkpoint restore | Avoid application initialization; still restore state, reattach I/O and satisfy compatibility. |
| Prestarted pool checkout | Claim an already-ready independent sandbox and bind the client; refill capacity separately. |

The user's few-millisecond objective is an explicit **target for local warm
checkout and the first small command**, not a claimed current result or a
promise for cold GNOME/Slurm startup. Remote RTT and client placement matter.
Current saved evidence includes roughly 0.56 s for a small CPU live restore,
multi-second full-desktop restore and millisecond Xvnc captures; there is no
validated millisecond cold sandbox creation today.

```python
from sandweave import Pool

with Pool(cache="coding-ready", size=32, warm=8) as pool:
    results = list(pool.map(evaluate, tasks))
```

`size` is maximum concurrent leases; `warm` is the requested idle-ready reserve,
subject to that total and real resources. Pool entry waits for the initial warm
reserve to become ready. Pools accept the same blueprint sources and launch
options as Sandbox. `map` runs `evaluate(env, task)` in caller-side worker
threads; commands/actions target the leased sandbox. It preserves input result
order by default and bounds outstanding tasks. The Python callback is not
serialized into the guest or moved to a remote worker. Async callbacks use
`map.aio`, which returns an async iterator; ordinary Sandbox I/O uses its `.aio`
methods. A callback failure releases its lease and is raised when its result is
consumed; `return_exceptions=True` returns a typed per-task outcome instead.
Closing the iterator stops further dispatch. Pool scope exit cancels/drains its
own remaining work and releases owned leases, reporting unresolved remote
outcomes instead of claiming cleanup succeeded.

Each lease is a fresh independent starting state. Used environments are never
returned to the clean reserve just because `/workspace` was deleted. Dispose
and replace them from the immutable baseline; an optimized restore/reset path
must prove equivalent state isolation before joining this contract. A snapshot
clone resets control-channel identity and reattaches external resources; copied
application RNG/time/external sessions are not magically independent. Templates
can expose explicit per-episode seed/reset hooks, and downstream tasks choose
their seeds.

Pools resolve a cache alias once and pin the revision for their lifetime. They
match the complete effective configuration, including resources, runtime,
capabilities, user, environment, mounts and network behavior. A mismatched
request cannot quietly take the wrong warm environment. GPU pools can prestart
fresh applications from filesystem caches without requiring GPU live snapshots;
their warm capacity consumes real VRAM. Empty pools wait or use an explicit
cold-fallback policy and report that path. A target-lost sandbox is never reused.

Implementation performance requirements:

- Keep workers, input channels and command control connections persistent;
  avoid spawning Apptainer/SSH/Python wrappers for every action or command.
- Keep image conversion, source hashing and preparation outside cache-reference
  checkout. Share immutable bases, with independent writable state.
- Use bounded shared-memory desktop/VR data paths locally; use explicit binary
  transport remotely. Do not base64 large frames through the control JSON API.
- Loading a capability should not import its heavy dependencies for coding-only
  use. Plugin dispatch must not add a remote round trip to every local action.
- Bound output, pending calls, pool queues and recorder buffers. Make backpressure
  and drops visible rather than letting latency/memory grow unbounded.
- Benchmark cold, filesystem, memory and warm paths separately at p50/p95/p99;
  include queue, provisioning, materialization, runtime, setup, capability-ready,
  first-command and first-observation timings. Include contention and steady
  refill demand. A local API-only microbenchmark is not end-to-end startup.
  Measure the default command-string path including its guest-shell startup,
  separately from explicit direct `argv` execution; do not silently parse a
  command string into arguments as a performance shortcut.

## 10. Internal module boundaries

| Module/protocol | Owns |
| --- | --- |
| `SandboxSpec` / template resolver | Versioned serializable configuration, merge rules, source fingerprints and required capabilities. No live resources during spec construction. |
| Runtime adapter | Create/exec/pause/snapshot/restore/terminate and enforcement report. Initial gVisor adapter; explicit Apptainer adapter exposes its smaller/different guarantees. |
| Target/allocation adapter | Local/SSH worker access, Slurm acquisition and capacity discovery; not guest desktop implementation. |
| Snapshot/artifact store | Immutable payload/dependency publication, aliases, checksums and retention; not application readiness. |
| Capability adapter | Guest helper lifecycle, readiness, action/observation schema and transport, recording and snapshot reattachment. |
| Pool/scheduler | Compatible placement, ownership, clean leases, bounded queues and refill; not agent policy/reward logic. |
| Python/CLI clients | Ergonomic calls over the same contracts. No duplicated CLI-only lifecycle logic. |

Plugins register through versioned Python entry points with namespaced options.
A capability descriptor declares its public schema, prerequisites, readiness and
snapshot hooks. Built-in `desktop` and `vr` use that same extension mechanism.
`env.capabilities` reports supported state kinds and unavailable features with
reasons. Unsupported combinations fail before mutating an existing sandbox.

A robotics plugin can expose reset, cameras, state, action stepping and reward
signals if its simulator supplies them. A Gymnasium/RL adapter can sit above it.
The generic sandbox core does not invent rewards, termination or deterministic
simulation stepping for an arbitrary interactive application.

## 11. CLI parity

The complete short CLI examples are in [the examples document](sandbox-api-examples.md).
Commands cover `create`, `exec`, `run`, `setup`, `shell`, `list`, `inspect`,
`pause`, `resume`, `stop`, `terminate`, `cache`, `snapshot`, `files`, `desktop`,
`vr`, `pool`, `targets` and `slurm`. `run` creates an ephemeral sandbox around
one command; `create` returns an explicitly managed environment ID.

Lifecycle and inspection accept `--json`; ordinary `exec`/`run` forward command
stdout/stderr and return its exit code. Control diagnostics go to stderr, never
corrupt image/process stdout. By default, `run`/`exec` require exactly one quoted
command string after `--`, passed unchanged to the guest shell, matching Python.
The caller quotes it for their local shell; the CLI never reconstructs it by
joining multiple arguments. Explicit `--argv -- PROGRAM ARG ...` selects direct
execution and matches Python's `argv=[...]`. Capability actions accept the same JSON schema as Python;
third-party capabilities can expose commands from their registered descriptors.

## 12. Implementation sequence and acceptance gates

This is a plan only. Each phase preserves current lab entrypoints and accepted
workloads; no phase is authorized to silently replace existing user environments.

| Phase | Work | Gate before claiming it works |
| --- | --- | --- |
| 1. Preserve the agreed contract | The README v1 examples, command-string interface, source/cache rules, readiness and ownership are agreed. Flesh out serialization and extension details consistently; seek an explicit contract revision for public behavior changes. | Implementation and public-repository docs conform to the README; experimental capabilities remain explicit. |
| 2. Package the existing mechanisms | SDK/CLI over current lifecycle, resource/network controls and gVisor; command/files interface; packaged coding/GNOME/VR templates; explicit cache references and preserved capability metadata. | Fresh, cached and restored examples pass in disposable sandboxes; no regressions in CPU live, GPU cold, desktop actions or both-eye VR. |
| 3. Optimize the training path | Persistent worker/control channels, preparation deduplication, async calls, bounded pool creation/refill and clean episode leases. | End-to-end latency distributions and isolation across leases; no stale action/eye observations or hidden output/recording loss. Millisecond goals remain goals until measured. |
| 4. Add placement adapters | SSH targets, existing Slurm jobs, explicit acquisition and pools across heterogeneous workers. | Respect allocated CPU/GPU boundaries, handle target loss and cache compatibility; never cancel unrelated jobs or silently resubmit actions. |
| 5. Exercise extension compatibility | Build desktop/VR through the public capability protocol; add a separate example simulator plugin and explicit volume/directory snapshot adapters. | Plugin supplies its schema/readiness/state semantics without modifying core APIs; documented unsupported combinations are rejected. |

Contract acceptance must also cover cache corruption/mismatch, concurrent builds,
changed setup inputs, nonzero command exits, execution versus wait timeouts,
unknown remote outcomes, creation cleanup, saved-before-stop failure, borrowed
handle lifetime and safe disposal of used pool environments. These are planned
tests, not newly executed tests in this design-only task.

Local foundations: [lifecycle](environment-lifecycle.md),
[resource/snapshot semantics](resource-snapshot-implementation.md),
[snapshot verification](asynchronous-snapshot-verification.md),
[GPU limits](gpu-filesystem-snapshots.md), [MPS](experimental-gpu-mps.md),
[desktop actions](xvnc-fast-io.md), [VR I/O](vr-continuous-io.md),
[current recording](gunspinning-vr-video.json). A generalized template registry,
content-addressed preparation cache, clean warm pool, distributed target
coordinator, plugin SDK and the new public API remain implementation work.

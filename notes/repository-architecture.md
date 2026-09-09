# Repository architecture proposal

Status: **architecture for review, not implementation**. This proposal preserves
the [agreed public API v1](../README.md#agreed-public-api-contract-v1), including
single command strings, ownership, cache semantics and both-eye VR observations.
No package skeleton, runtime changes or repository migration accompanies it.

## 1. Six pillars in one package

Use one installable Python package, `sandweave`, organized around six concepts.
Each pillar has a primary unit and a boundary through which others use it.
An atomic unit here means a thing with its own identity or complete value,
invariants and lifecycle, which can be composed and tested independently. It
does not imply one class, process or file for every noun.

| Pillar | Primary unit | Responsibility | Boundary |
| --- | --- | --- | --- |
| Templates | `ResolvedTemplate` | Describe the starting environment: base, preparation, services, readiness, defaults and capabilities. | Resolve a recipe and declared inputs into immutable, pinned configuration. |
| Environments | `SandboxRecord`, addressed by `SandboxRef` | Coordinate one sandbox's lifecycle, ownership, commands, files and capability bindings. | The agreed `Sandbox` API and the corresponding worker operations. |
| Runtimes | `InstanceRef` | Execute and isolate one guest; implement process, resource, pause and capture/restore mechanisms. | A runtime driver consumes launch/command requests and returns instance/process/capture references. |
| Targets | `WorkerRef` | Locate/connect to a worker and discover its capacity; optionally acquire an allocation explicitly. | A target adapter supplies a worker connection, capability report and allocation ownership information. |
| Snapshots | `SnapshotRef` plus its manifest | Store immutable reusable state, dependencies, aliases and verification metadata. | Resolve, materialize, publish and verify an artifact revision. |
| Capabilities | `CapabilityBinding` | Supply a domain interaction contract and attach it to one sandbox. | A descriptor, typed session, readiness checks and lifecycle hooks. |

`Pool` belongs to environments: its supporting unit is a lease on an independent
sandbox. A command is a process within a sandbox; file operations use the same
sandbox identity. The Python API and CLI are clients of these concepts. They do
not need separate implementations of lifecycle rules.

This preserves all boundaries in the [API design](sandbox-api-proposal.md#10-internal-module-boundaries):
pooling is grouped with environments, while clients are thin entrypoints.
The number of pillars is independent of the number of deployed processes.

## 2. Repository layout

The following is the proposed public-repository layout, not directories to
create preemptively. Start a module as one file where practical and expand a
folder only when its implementation warrants it. Routine `__init__.py` files
are omitted from the tree except the public entrypoint.

```text
sandweave/
├── README.md                         # Agreed API and getting started
├── pyproject.toml                    # One package, CLI, optional dependencies
├── src/sandweave/
│   ├── __init__.py                   # Small, lazy public exports
│   ├── contracts.py                  # Shared records, errors and adapter protocols
│   ├── plugins.py                    # Lazy discovery/registration of adapters
│   ├── cli.py                        # Arguments/output -> public API
│   ├── worker.py                     # Compose adapters; serve worker operations
│   ├── environments/
│   │   ├── sandbox.py                # Public Sandbox handle and connection
│   │   ├── lifecycle.py              # Worker-side state and operation ordering
│   │   ├── process.py                # Command normalization, Process, streams
│   │   ├── files.py                  # File API and streaming
│   │   └── pool.py                   # Pool, leases, bounded placement/refill
│   ├── templates/
│   │   ├── resolve.py                # Load, merge, validate, fingerprint
│   │   ├── coding/template.toml
│   │   ├── gnome/
│   │   │   ├── template.toml
│   │   │   └── setup.sh
│   │   └── vr/gunspinning/
│   │       ├── template.toml
│   │       ├── setup.sh
│   │       └── start.sh
│   ├── runtimes/
│   │   ├── gvisor/                   # Driver, resource/network/device mechanisms
│   │   │   └── native/               # Pinned source manifest, patches, build recipe
│   │   └── apptainer.py              # Explicit native execution adapter
│   ├── targets/
│   │   ├── local.py
│   │   ├── ssh.py
│   │   └── slurm.py
│   ├── snapshots/
│   │   ├── manifest.py               # Identity, dependencies and compatibility
│   │   └── local.py                  # Initial durable store implementation
│   └── capabilities/
│       ├── desktop/                  # Desktop schema, Xvnc/Wayland, native helper
│       └── vr/                       # Stereo/controller schema, Monado, recording
├── tests/
│   ├── contracts/                   # Shared behavioral checks for adapters/API
│   ├── unit/                        # Meaningful local invariants
│   └── integration/                 # Disposable runtime/desktop/VR acceptance
├── benchmarks/                      # Startup, commands, actions and pool throughput
├── examples/                        # Small programs using only the public API
├── scripts/                         # Development/build/job convenience entrypoints
└── docs/                            # Architecture, templates, plugins and limits
```

All six pillars live at the same package level. `contracts.py` contains only
values and protocols actually exchanged across boundaries: specs, identities,
resource requests/reports, operation results, references and common errors.
Keep desktop actions and VR observation types in their capability packages.
Keep local implementation details beside their owner. Split `contracts.py` into
a package only if its size justifies it, retaining its import surface.

Templates have one canonical location, inside the installable package, and ship
with their TOML/scripts as package data. Thus a wheel installation can create a
built-in environment without locating this Git checkout. A user's local template
or setup script can live anywhere; it does not need to be copied into the
library or registered globally. Build acceptance must check installed package
resources from a directory outside the checkout.

Native helpers and upstream build recipes live with the runtime or capability
that uses them. Authoritative gVisor source remains a pinned upstream/fork
revision with the authored patches and build metadata retained in version
control. Downloaded checkouts, built binaries, images and snapshots go into
configured data/cache locations, not the source package. There is no runtime
dependency on a developer's `scripts/` directory or absolute lab path.

## 3. The units inside each pillar

### Templates: describe preparation and startup

A `Template` is the user-facing recipe; `ResolvedTemplate` is its immutable
result after inheritance, overrides and input identity have been resolved.
Its useful constituent values are:

- A base artifact reference.
- `SetupStep`: guest script, interpreter/user and declared input artifacts.
- `ServiceSpec`: guest entrypoint, prerequisites and readiness probe.
- Capability descriptors/configuration and resource defaults.

These are data. Loading a template does not acquire a GPU, start a shell on the
host or import a game's implementation. Preparation runs through the same
guest execution contract as other operations. A setup script's interface is
its declared files/environment, interpreter, exit status and captured logs;
there is no mandatory Python subclass or custom workflow language.

For example, adding an application to GNOME creates this template directory:

```text
src/sandweave/templates/my-desktop/
├── template.toml
└── setup.sh
```

```toml
schema_version = 1
extends = "gnome@1"

[setup]
script = "./setup.sh"

[resources]
cpu = 4
memory = "8GiB"

[capabilities.desktop]
resolution = [1280, 800]
```

The catalog resolves its name from the directory and manifest; adding this
template does not require editing `Sandbox` or a central application switch.
Only a setup script is needed for `Sandbox(setup="./setup.sh")`. More elaborate
templates can declare additional services using the already agreed TOML schema.

Paths in manifests are resolved relative to the manifest. For remote targets,
declared local files are hashed/transferred as artifacts before guest setup;
a local path is not assumed to exist on a remote node. Preparation-cache keys
include the resolved recipe and inputs. Worker-side validation confirms the
same identities before using a shared preparation result.

### Environments: own lifecycle decisions

The public `Sandbox` is a client handle. The authoritative `SandboxRecord`
contains the ID, resolved spec, worker/runtime references, lifecycle generation,
owned resources, capability bindings and operation outcomes. Multiple handles
may refer to the same record; closing a handle is not terminating that record.

`lifecycle.py` coordinates create, readiness, pause, capture, restore and stop.
It knows the adapter contracts, rather than GNOME or GunSpinning application
names. Runtime-specific mechanisms remain in the driver. Domain-specific
detach/reattach behavior remains in capability hooks.

`ProcessRef` identifies a guest process/owned process group and its stream
attachments. `ExecRequest` carries arguments, user, cwd, environment and limits.
The command-string API is normalized once on the worker: a string becomes
`[guest_shell, "-c", original_string]`; explicit `argv` remains literal. The
runtime receives an argument vector and never re-parses shell text. This is an
internal boundary, not a change to the agreed string-first public interface.

Files are streamed through an environment file channel, without assuming that
guest paths equal host paths. The runtime exposes the required primitive or
installs a qualified guest helper; users see the same `env.files` contract.

A pool lease records an owned sandbox, pinned baseline and effective launch
configuration. The pool schedules caller-side callbacks and manages clean
capacity; it does not implement a second create/reset/terminate algorithm.
Leases do not introduce a hidden automatic TTL. Cleanup and allocation ownership
follow the agreed API even after a client disconnects or an outcome is uncertain.

### Runtimes: own mechanisms

An `InstanceRef` identifies one guest plus its runtime build/generation. A driver
creates/restores it, starts processes, supplies file access, applies supported
resource/network controls, pauses/resumes, captures state and terminates it.
It returns requested/effective settings and enforcement limits explicitly.

CPU counts/weights, memory budgets, network policy and GPU selection are shared
request values, not separate application pillars. Their enforcement belongs to
the runtime and its worker-local helpers. A shared CPU broker outlives individual
guests when peers still use it; cleanup is based on explicit resource ownership.

The gVisor driver may use Apptainer as an unprivileged host wrapper. That is
distinct from choosing `runtime="apptainer"` for native execution. Targets need
not duplicate either driver. Official adapters preserve the no-host-sudo/no-KVM
requirement and report unsupported combinations rather than switching isolation.

### Targets: own worker access and allocation

A `WorkerRef` names one reachable Linux worker and its generation. `WorkerInfo`
describes architecture, supported drivers/capabilities, allocated devices,
eligible CPUs, budgets and accessible stores. A worker admits resource requests
against its current state; a cached capacity report is not a reservation.

`local` connects to the current host/allocation. `ssh` connects to an explicit
worker and carries control/bulk channels. `slurm` discovers/connects to existing
allocations or performs separately requested acquisition. Slurm supplies hosts
and resource boundaries; it is not part of the sandbox's guest execution code.

An `AllocationRef` records an existing allocation's identity and limits; an owned
allocation lease additionally authorizes its release. A sandbox resource lease
does not confer ownership of the entire Slurm job. Site partition/account/QoS
settings belong in configuration, not in the generic package.

### Snapshots: own reusable state and its identity

`SnapshotRef` addresses an immutable manifest and its payload dependencies. The
manifest records filesystem versus memory state, template/capability metadata,
compatibility constraints, provenance and the verification-record identity.
Changing verification status lives in a separate record; it does not mutate the
captured manifest or payload. Aliases and preparation keys resolve to revisions;
they are not the payload's identity.

The runtime captures/restores bytes and runtime metadata. The store publishes,
resolves, verifies and materializes them. The environment coordinates when a
capture is permitted and whether the source should stop afterward. The store
does not freeze processes or start desktops. Filesystem materialization still
requires a fresh application start; memory restore follows its compatibility
and reattachment contract.

Keep one initial store implementation. A later remote/object store implements
the same publication and alias semantics. Shared storage alone does not provide
distributed locking: multi-worker preparation deduplication and compare-and-swap
need a qualified metadata authority/backend. Do not assume a local file lock or
SQLite file is a correct cluster-wide coordinator merely because a path is shared.

### Capabilities: own domain interaction

A descriptor specifies a name/version, prerequisite features, action/observation
schemas and hooks. A `CapabilityBinding` is one attached session with its own
generation and ownership. The worker holds its authoritative binding record;
the client exposes a typed session using negotiated channel references. Generic
prerequisites are validated before attaching.
The binding receives guest execution/files, named service endpoints and negotiated
data channels through a small `CapabilityContext`, not the lifecycle manager's
private methods or filesystem layout.

Start services and attach capability helpers in their declared prerequisite
order. For a desktop, the display service can precede its input helper; for VR,
Monado can start first, the game next, and paired-frame readiness last. Binding
and final observation readiness are separate phases: attaching a helper must
not wait for an app that has not been started yet. Validate missing prerequisites
and dependency cycles before launch. This needs a small dependency order, not a
general workflow engine. Each helper/service has one recorded owner.

Lifecycle hooks cover input release, recording/stream draining, detachment,
reattachment and close. Memory restoration reconnects to supported saved guest
processes; it does not rerun cold-start hooks and launch duplicate applications.
Unsupported active attachments make capture fail explicitly. The orchestration
layer runs hooks in dependency order and can restore the previous usable state
after a failed save.

`desktop` owns keyboard/mouse schemas, Xvnc and the optional Wayland path. `vr`
owns head/controller schemas, stereo observations, Monado integration and paired
recordings. Required display/service endpoints are supplied explicitly. A new
robotics plugin can expose simulator-specific stepping without changing either
capability or imposing an RL task schema on the core.

## 4. How the pillars communicate

Keep the internal interfaces small and behavioral. The following names describe
the proposed internal boundaries; they are not additional public SDK methods.

| Boundary | Input -> output | Required semantics |
| --- | --- | --- |
| Template resolution | Source plus overrides -> `ResolvedTemplate` | Deterministic interpretation of declared inputs; no live resource allocation. |
| Target connection | Target selector -> `WorkerClient`, `WorkerInfo` | Connect to the chosen worker; new allocation only when explicitly requested. |
| Runtime operations | Launch/exec/capture requests -> instance/process/capture references | Report supported behavior, identity and effective enforcement; preserve owned-process scope. |
| Artifact storage | Manifest/payloads or reference -> published/materialized revision | Immutable identity, complete dependencies, explicit verification and atomic alias updates. |
| Capability attachment | Descriptor plus context -> binding and readiness | Versioned schemas, explicit prerequisites and lifecycle hooks. |
| Worker operations | Versioned operation request -> result, error or operation status | Authoritative ownership/state; reconcile uncertain outcomes; no blind replay. |

Calls exchange immutable records/references rather than reaching into another
pillar's private objects. A reference contains identity and location information
appropriate to its boundary; it is not just an unqualified local filesystem path.
Domain frames and large file/process data use bounded bulk channels. Serializable
channel descriptors distinguish local shared memory from a remote binary stream.
Host pointers and file descriptors cannot simply be serialized across nodes.

The import direction is explicit:

- `contracts.py` imports no concrete runtime, target, capability or client.
- Each adapter imports shared contracts and its own implementation dependencies.
- `environments` orchestrates injected interfaces and the pure template resolver;
  it does not import a particular game, display implementation or Slurm driver.
- `plugins.py` resolves registered names lazily. `worker.py` composes selected
  adapters and serves the same lifecycle implementation for every target.
- The Python handle and CLI use the same worker contract; CLI parsing/output
  does not contain cache, pause, cleanup or resource policy.

Public `Sandbox`/`Pool` creation can use a small connection factory with the lazy
target registry. Neither adapter discovery nor `import sandweave` should launch
processes, import graphics libraries, probe GPUs or require cluster configuration.
Versioned Python entry points allow separately installed adapters; built-ins use
the same registration path. Public exports remain the agreed ones.

## 5. One creation, end to end

For `Sandbox(template="gnome", setup="./install-tools.sh", cache_key="tools")`:

1. Normalize the agreed arguments, resolve declared local inputs and select the
   explicit/default target. Connect to its worker.
2. The worker validates the resolved request against actual capabilities,
   resolves preparation reuse and admits the required resources. Record the
   operation and ownership before making live changes.
3. The store supplies pinned artifacts. On a preparation miss, the runtime starts
   the base and executes setup in the guest. On a hit, restore the saved baseline.
4. Template services start and capability helpers attach in their resolved
   prerequisite order. The environment then waits for declared service/capability
   readiness. Memory restoration instead follows the qualified reattachment path.
5. Publish a newly prepared cache before exposing the mutable environment, then
   return the ready handle. Failures retain diagnostics and preserve the agreed
   cleanup/save rules.

For `env.cache(...)`, the same environment controller coordinates hooks and
runtime capture, then asks the store to publish the manifest/dependencies. For
`env.stop()`, it releases the runtime only after that save succeeds. A pool
reuses these operations and pins complete compatible baselines.

The hot action path is shorter: the bound capability sends an action through its
persistent channel and receives its typed acknowledgement/observation. It does
not resolve the template, query Slurm or walk the store on every action.

## 6. Scaling without multiplying implementations

Start with a persistent unprivileged worker per host/allocation context. The
worker owns local sandbox records, operation coordination and shared helpers;
it does not have to disappear when a client disconnects. Its client connection
and its running environments have separate ownership. No mandatory public
server, cloud account or separate distributed service deployment is introduced.
Local connections can use a private socket; SSH uses an explicitly authenticated
remote connection. Both reach the same worker operations.

Local pools select capacity on one worker. Multi-target pools coordinate leases
across workers, with admission enforced by each worker and compatible snapshot
access. A sandbox stays on one worker. Relative CPU weights remain local, and
an environment does not acquire CPUs spread across machines. Multiple clients
must share authoritative worker admission rather than overcommitting based on
independent stale reports.

The callback in `Pool.map` stays in the caller's Python process as already
agreed. Scheduling metadata is small; observations, files and process streams
use bounded channels. Used guests are replaced with independent baselines, and
refill is measured separately from checkout. A lost worker invalidates its
references; resuming or replaying workload actions is not automatic.

Resource bookkeeping and lifecycle serialization live on the worker. Heavy
snapshot hashing, image preparation and recording finalization must not block
all workers' command dispatch. Use bounded background work with explicit
outcomes. No particular async framework, database or RPC library is being
chosen by this folder layout. Sync and `.aio` clients must share operation
semantics, including cancellation and unknown remote outcomes.

## 7. What changes when we extend it

| Desired addition | Expected change |
| --- | --- |
| Another app using the desktop/VR interface | Add a template manifest and optional setup/start files. |
| Another display implementation | Add an implementation under `capabilities/desktop`, preserving the desktop schema and explicit selection. |
| A simulator with a new interaction model | Add/install one capability plugin plus its template, with its own schema and timing semantics. |
| Another execution engine | Add a runtime driver and its declared support; reuse targets, store and lifecycle. |
| Another cluster/provider | Add a target/allocation adapter; reuse compatible runtime drivers. |
| Another artifact storage backend | Add a store adapter with the same identity/publication guarantees. |
| More concurrent jobs | Configure more eligible targets/resources and pool capacity; retain the same downstream callback/API. |

Adding an implementation registers it with the relevant adapter group; it does
not edit a global switch statement for every application/runtime/provider
combination. An incompatible feature can fail explicitly without weakening the
shared contract. Optional graphics/VR dependencies load only for their adapters.

`scripts/` contains thin human convenience commands: developer setup, building
selected native dependencies, or submitting a site-configured job. Reusable
Slurm acquisition belongs in `targets/slurm.py`; reusable engine build logic
and manifests belong beside that engine. Examples use the public API and do
not import implementation internals.

## 8. Mapping the existing lab into this layout

The current source already contains useful modules, alongside orchestration
that assumes a particular lab directory or concrete backend. This mapping is
an extraction plan, not a claim that moving files would implement the SDK.

| Existing source | Destination/responsibility |
| --- | --- |
| [environment.py](../scripts/environment.py), [environment_control.py](../scripts/environment_control.py) | Environment lifecycle/ownership and qualified coordination; separate client handles from worker authority. |
| [run-gvisor.py](../scripts/run-gvisor.py), [make-gvisor-bundle.py](../scripts/make-gvisor-bundle.py) | gVisor driver launch and bundle construction. |
| [cpu_broker.py](../scripts/cpu_broker.py), [gvisor_gpu.py](../scripts/gvisor_gpu.py), [gvisor_mps.py](../scripts/gvisor_mps.py), [network_policy.py](../scripts/network_policy.py) | Resource policy values plus runtime enforcement and shared worker helpers. |
| [checkpoint-gvisor.py](../scripts/checkpoint-gvisor.py), [filesystem_snapshot.py](../scripts/filesystem_snapshot.py) | Runtime capture/restore primitives, invoked through environment lifecycle coordination. |
| [snapshot_store.py](../scripts/snapshot_store.py), [runtime_store.py](../scripts/runtime_store.py) | Immutable artifact identity, publication and integrity; runtime compatibility remains explicit. |
| [fast_io.py](../scripts/fast_io.py), [xvnc-fast-io.c](../sources/xvnc-fast-io.c) | Desktop capability and its native helper. |
| [vr_stream.py](../scripts/vr_stream.py), [vr_input.py](../scripts/vr_input.py), [vr_stream_guest.py](../scripts/vr_stream_guest.py) | VR capability, transport, guest helpers and recording. |
| App installation/start scripts | Template-owned setup/start files; shared mechanisms stay in their owning adapter. |
| Existing `env.py`, `fastio.py` and VR CLI wrappers | Initially retained lab interfaces; the new CLI calls the packaged API once implemented. |

For example, the current `EnvironmentManager` calls `fast_io.detach` directly,
and `VRStream` calls manager-private methods to construct gVisor commands. The
new boundaries replace those couplings with capability lifecycle hooks and
guest execution/data-channel interfaces. Their tested native behavior should
be carried forward and requalified through those interfaces.

Keep this lab, its Git history and live environments intact during extraction.
Build the eventual public package from deliberately selected source and examples;
runtime state, credentials and site paths are configuration/data rather than
package structure. Preserve engine commit/build provenance from
[source-revisions.json](source-revisions.json). Moving a script is insufficient
if it still shells out to files in this checkout.

## 9. Implementation discipline after architecture agreement

Create files when their behavior is implemented. The first complete path should
cover the agreed coding example through local target, gVisor runtime and common
process/files/lifecycle contracts. Extract the established snapshot and desktop
behavior next, then VR and pools/remote targets following the API plan. Introduce
each adapter protocol at its first real use and qualify the second implementation
against it; do not fill the tree with speculative abstract classes or empty plugins.

Acceptance belongs to behaviors across boundaries: failed-save preservation,
borrowed versus owned handles, process/stream timeouts, cache identity, changed
inputs, clean pool leases, capabilities surviving restore and actual both-eye
VR output. Reusable adapter tests check these contracts against each driver's
declared support, including explicit rejection of unsupported operations.

Benchmarks cover usable cold/cached/memory/warm readiness, the default command
string's guest-shell cost, first useful work, action latency, throughput and
contention. Measure p50/p95/p99 without transferring previous lab measurements
to an unimplemented architecture. Documentation examples and built-in package
resources must work from an installed distribution before a public release.

The criterion for another folder or abstraction is an independently changing
responsibility with a concrete caller and testable contract. Keep local helper
functions with their owner; only extract shared machinery when actual callers
need the same semantics. This leaves the code small initially while preserving
the boundaries that additional games, engines, hardware and workloads need.

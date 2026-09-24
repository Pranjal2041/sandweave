# Changelog

## 0.2.27

- Support Linux 5.4 workers with a compatible prebuilt gVisor engine. Fall back
  from unavailable `openat2` without following symlinks or crossing the overlay
  filestore's mount boundary. Preserve guest GS state on hosts without FSGSBASE.
- Upgrade incompatible prepared engines automatically while reusing guest
  images. Keep the native `openat2` path on newer kernels.
- Check worker architecture and the supported kernel range before installation
  in doctor, setup and local first use.
- Use the sandbox's startup deadline when activating its guest service, including
  slow systemd startup; wait for agent readiness without a separate 30-second cap.
- Stop GNOME's physical-device discovery from blocking virtual desktop startup
  in a udev restart loop.

## 0.2.26

- Open a bidirectional VNC byte stream with `env.desktop.vnc()`, including
  `Sandbox.connect` handles on local, SSH and Weave targets. Provide `read`,
  `write`, `close` and async equivalents through the authenticated SDK endpoint.
- Forward streams asynchronously through controllers and outbound worker
  relays with bounded buffering and backpressure. Preserve sandbox-scoped
  authorization and close streams on handle closure, pause, termination or
  declared worker loss.

## 0.2.25

- Preserve the saved runtime-memory budget when restoring a snapshot or cache
  with a string or byte-count guest-memory override. Explicit `Memory(...)`
  settings still override the saved budgets.

## 0.2.24

- Create a service network with `create_service_network(target=...)`, then join
  independent sandboxes with `service_network`, `aliases`, and `networks`.
  Coding, desktop and custom gVisor templates use the same API.
- Pin members to the network's worker. Private traffic uses the existing worker
  hub with fixed source identities and named-network isolation. Alias discovery
  supports later joins; ordinary egress policies still govern outside traffic.
- Persist membership across worker restarts, remove routes on termination, and
  delete empty networks with `delete_service_network`. Filesystem restores get
  fresh member routes; independent memory rollback of a member is rejected.

## 0.2.23

- Extract the trusted Apptainer tools filesystem directly with its installed
  SquashFS extractor. Avoid the internal build container that dropped mount
  exclusions and failed when optional host files such as `/etc/localtime`
  were absent. Keep atomic cache publication and reuse across launches.
- Qualify fresh host-image extraction and launch in installed-package release
  acceptance, in addition to the sandbox lifecycle tests.

## 0.2.22

- Run inside existing container user namespaces when the caller already has
  container-local mount authority. Keep ordinary unprivileged host launches
  unchanged and skip optional Apptainer binds when their source is absent.
- Reuse an unpacked Apptainer host image inside those containers, avoiding
  nested FUSE mounts and repeated extraction on each sandbox launch.
- Honor explicit OCI user namespaces, handle mediated mount paths and proc
  cleanup, and use gVisor's existing futex communication when an outer
  container already owns the seccomp notification listener.
- Add concurrent sandbox and process-checkpoint acceptance under an inherited
  seccomp listener. No host sudo, KVM or host security-policy changes are used.

## 0.2.21

- Default controller listeners to `127.0.0.1` instead of `0.0.0.0`, including
  startup with TLS or a credential file. Explicit listener addresses and saved
  listener settings keep their existing behavior.
- Update connection instructions for local HTTP and remote SSH access by default.
  Direct remote HTTP or HTTPS remains available through an explicit `--listen`.

## 0.2.20

- Publish the tested `0.2.20rc8` implementation as a stable release. Runtime code
  and dependency requirements are unchanged from that release candidate.
- Add capacity-bounded benchmark task acquisition, OSWorld setup and evaluation,
  and experimental Harbor integration. Harbor remains explicitly experimental;
  this release does not claim full Harbor compatibility or full OSWorld VM parity.
- Include the image-build, service lifecycle, long-path, networking and FIFO
  fixes documented in the preceding release candidates.
- Save directories bind-mounted onto themselves and preserve Docker/containerd
  storage across checkpoints, including an empty initial store without an archive.
- Upgrade clients, controllers and workers together for the benchmark and service
  operations introduced in this release.

## 0.2.20rc8

- Allow filesystem checkpoints when an application bind-mounts a directory onto
  itself. Save its backing filesystem once, while retaining detection of other
  writable storage that would be omitted.
- Honor the template's `runtime_options.docker_data` independently of archive
  import. The Docker template starts with empty Docker and containerd storage
  by default; no pre-existing archive is required.
- Preserve Docker and containerd storage through filesystem and memory
  checkpoints. Restore uses the saved data without reopening or reimporting
  the original Docker archive.
- Add installed-package acceptance for directory self-binds, concurrent
  checkpoints, empty Docker storage, images, containers, volumes and imports.

## 0.2.20rc7

- Match Harbor's environment precedence at the client command boundary and
  isolate sidecar execution from the main agent's user, working directory and
  scoped variables. Minimal sidecars use their POSIX shell.
- Apply Harbor's resource-policy Compose overlay in the upstream order, honor
  absent optional dependencies, and retry timed-out service health probes.
- Pin Compose service image tags through the benchmark's shared resolver so
  different workers receive the same immutable image revision.
- Give intermediate separate verifiers independent lease capacity so they can
  use the agent's image while the agent remains alive. Image preparation stays
  deduplicated. Slow warm-task cleanup no longer holds the capacity lock.
- Pass native and ATIF trajectory inputs and multi-step continuation context to
  the client through `task.env.harbor`.
- Propagate multi-step verifier failures instead of returning an earlier reward.
  Honor disabled verification during task discovery and execution; evaluations
  explicitly report `skipped=True` without inventing a reward.
- Include contract-based Harbor tests in installed-wheel release acceptance,
  covering cross-feature interactions and Harbor's own Oracle agent.

## 0.2.20rc6

- Support deeply nested and multibyte storage paths for Unix sockets during
  first-use installation, networking, service routing, command connections,
  CPU control and desktop I/O. Socket files remain in their original storage.
- Launch the network helper from its private socket directory, so a long
  `TMPDIR` does not prevent runtime setup. No benchmark-specific paths or rules.
- Exercise long temporary paths during installed-package release acceptance,
  including automatic setup, Dockerfile builds and cached-image reuse.
- Store BuildKit layers, build contexts and OCI output on an owned worker
  volume instead of consuming guest RAM. Remove build storage on completion
  and failed builds through the existing sandbox cleanup lifecycle.
- Use overlay snapshots during Dockerfile builds instead of copying the whole
  filesystem for every layer. Preserve guest device metadata and trusted
  directory attributes on private volumes without creating host devices.
- Preserve character and block device numbers when importing EROFS images.
- Keep unrelated sandbox admission working while an image import is starting.
- Import completed build archives directly from their owned worker volume,
  avoiding a full-image copy through the guest network connection.
- Cache metadata for builder-owned volumes, avoiding repeated host filesystem
  checks across image layers. Flush output before import; service volumes shared
  by different sandboxes retain shared access.
- Keep overlay directory markers consistent between writable build steps and
  read-only multistage copies, so deleted files cannot reappear in copied trees.
- Support legacy IPv4 and IPv6 iptables state rules using the existing
  connection tracker, including first replies and related ICMP errors.
- Support standard IPv4 and IPv6 firewall rejection modes, including network
  unreachable and administrative denial, with the corresponding ICMP replies.
- Recheck pool state before capturing its baseline so a stale reconciliation
  cannot capture an already-stopped builder or reopen a closing pool.

## 0.2.20rc5

- Resolve Harbor datasets through its native registry and package protocols,
  including Terminal-Bench 3. Preserve source revisions, task filters, original
  setup, multi-step verification, artifacts and named rewards.
- Build original Dockerfiles with isolated BuildKit, including remote and inline
  contexts, build arguments, stages, additional contexts and secret mounts.
  Read registry credentials from Docker configuration and credential helpers.
- Run Compose service groups directly in Sandweave, with service DNS, health
  checks, dependencies, private shared volumes, guest file ownership, secrets,
  read-only roots, restart policies and graceful artifact-preserving shutdown.
- Apply phase-specific public, offline and IPv4 allowlist networking. Expose
  Harbor MCP definitions, skills paths and agent result context to client loops.
- Drain cancelled trial cleanup before returning capacity. Keep image build and
  import reservations, group placement and failed-launch cleanup coordinated.
- Grant local pool capacity to waiting callers in arrival order, so a later
  checkout cannot hold the available slot while an earlier caller waits.
- Honor Harbor CPU quotas and acceptable GPU model lists. Preserve image ENV
  when restoring with additional environment values.
- Include the engine support for private service volumes. Upgrade clients,
  controllers and workers together for the new worker operations.
- Fix TCP payload corruption and stalled replies after network backpressure.
  Install a qualified passt helper automatically, including when the host
  already has passt. Preserve launcher identity across process execution.
- Allow sandbox cleanup after an external mount source has been removed.
- Honor SA_RESTART for interrupted blocking opens, preventing intermittent
  FIFO startup failures in image builds.
- Prepare the runtime automatically when a Dockerfile build is the first
  operation in a new installation, including Harbor tasks that need a build.

## 0.2.20rc4

- Add Harbor datasets and local task directories through `Benchmark("harbor",
  source=...)`. Keep Harbor's task loader, trial lifecycle, verifier scripts,
  timeouts, artifacts and native reward metrics.
- Share one capacity and warm-reserve budget across different task images. Pin
  each image once per benchmark and deduplicate baseline preparation. Keep launch
  waits separate from file transfers and cleanup.
- Support explicit multi-step pulls with `task.next_step()`, preserving the guest
  between steps. `task.evaluate()` returns Harbor metrics in `result.rewards`.
- Add a Harbor environment provider using direct Sandweave guests, including
  separate verifier environments and archive-based file transfer. Initial support
  requires prebuilt public Linux amd64 images; see the documented runtime limits.
- Preserve the existing OSWorld task and evaluation API.

## 0.2.20rc3

- Add `next(bench)` and `bench.next(timeout=...)` to acquire prepared tasks from
  a shared cursor. Concurrent callers respect pool capacity; a timed-out checkout
  leaves the task available. Evaluation remains explicitly controlled by the client.
- Add `task.env` and idempotent `task.close()`. Closing a benchmark environment
  releases its task lease too. Benchmark shutdown drains setup and evaluation
  before releasing task resources.
- Add cancellable `bench.next.aio()` with bounded acquisition threads, so
  capacity waiters cannot occupy the executor used by cleanup and unrelated calls.
- Support timed checkout on local pools and explicit unlimited checkout on
  cluster pools. Existing iteration, mapping, worker RPCs and engine are unchanged.

## 0.2.20rc2

- Enable Avahi address notifications, console palette access and fixed desktop
  sysctl support through the OSWorld template. Its original `avahi-daemon`,
  `setvtrgb` and `systemd-sysctl` units can run without editing their service files
  or the shared base image.
- Preserve the console palette, route-netlink subscriptions and desktop PID
  range across live snapshots. Other templates retain their existing defaults.
- Keep unsupported sysctl values rejected. The selected desktop PID range is
  enforced by the guest allocator; host sysctls are unchanged.

## 0.2.20rc1

- Add `Benchmark`, with capacity-bounded task leases, instructions, sync/async
  agent mapping, and evaluations. Use the existing pool lifecycle for cleanup
  and independent starting state.
- Add the OSWorld integration using the pinned `cua-speed-run` desktop recipe,
  task setup and canonical verifier. Prepare the original Ubuntu disk directly;
  keep private reference dependencies and verifier code outside the public package
  and agent sandbox. The representative split requires reference repository access.
- Wait for task application/document windows after GUI launch commands so agents
  receive a loaded desktop instead of racing background application startup.
- Add opt-in headless virtual consoles, user keyrings and FUSE truncate support
  to the engine so the original GDM and GNOME session can start. Preserve console
  state across live snapshots and enforce guest console permissions.
- Share fallback engine builds across workers and reuse disk-image verification
  receipts across pool/worker staging. Existing desktop and command APIs remain
  unchanged. Full VM parity is not claimed; see the OSWorld acceptance record.

## 0.2.19

- Release a pool lease even when its checkout acknowledgement is lost. Retry
  uncertain idempotent release requests within the pool's wait timeout while
  preserving the original checkout or episode error.
- Reuse a worker's existing snapshot before accessing a shared cache. Only
  missing revisions enter the deduplicated publication and transfer path.
- Preserve builder affinity after termination and controller restart without
  charging the released builder against worker resources.
- Keep pending pool polling at 50 ms, bound each wait by the remaining deadline,
  and wake lease polling on cancellation.
- Upgrade clients for lease cleanup and restart upgraded controllers for image
  reuse and placement. Public APIs, worker RPCs and the engine are unchanged.
  Live cluster checks passed against unmodified 0.2.16 workers.

## 0.2.18

- Wait for worker lifecycle RPCs asynchronously. Concurrent creates, claims,
  termination, observations, baseline capture, cleanup and job operations release
  controller threads while waiting for replies over HTTP or outbound relay.
- Keep each allocation serialized until its operation and final state commit
  finish. Preserve assignment generations, lost-worker cancellation, shared-image
  preparation and shutdown draining.
- Serve pool lease polling and projected pool/allocation status from indexed
  memory, outside the writer lock and control executor. Failed pool polls report
  the failure immediately; reconciliation persists cleanup separately.
- Upgrade and restart the controller to apply these changes. Public APIs, worker
  RPCs, saved controller state and the engine binary are unchanged.

## 0.2.17

- Make staged NVIDIA EGL and Vulkan configuration files readable by guest
  desktop users, including when workers use a restrictive umask or reuse a
  previously staged driver. This fixes graphics initialization failures while
  `nvidia-smi` still works. Public APIs and the engine binary are unchanged.

## 0.2.16

- Isolate worker subprocess launches from open storage handles. A dedicated
  launcher passes only the descriptors each command requests; a slow filesystem
  flush during image publication no longer stalls unrelated sandbox starts.
  This applies to every filesystem, with no provider-specific settings.
- Preserve command streams, timeouts, signals, sessions, passed descriptors and
  the worker's current resource settings through the launcher.
- Correct native snapshot import paths and preserve guest symlink metadata.
- Allow a new local sandbox to start when the previous idle worker is completing
  its acknowledged shutdown.
- Upgrade and restart workers to apply the launcher fix. Public APIs and the
  engine binary are unchanged.

## 0.2.15

- Cancel outstanding HTTP and relay requests when a worker is explicitly marked
  lost. Exclude its current and historical endpoints from image fetching and
  pool cleanup, including after a controller restart.
- Share one image-preparation future per destination cache and snapshot.
  Waiting sandboxes release their launch threads while publication completes.
  Unrelated launches and termination continue independently.
- Reuse verified file checksums through snapshot publication, import and runtime
  staging. Newly copied bytes are checked; explicit snapshot verification still
  reads the payload. Accept hard-link metadata changes during the first base-image
  verification while retaining strict checks for unhashed checkpoint sources.
- Upgrade controllers and workers to apply these fixes. Public sandbox and pool
  arguments are unchanged.

## 0.2.14

- Add opt-in desktop recording with `recording=True` or `Recording(fps=15,
  cursor=True)`. Capture runs independently on the worker, between agent actions.
- Retain fragmented MP4 videos, capture timestamps and dropped-frame metadata
  outside the guest. Finalize before cleanup and allow downloads after sandbox
  termination, including partial recordings after failures.
- Pause and snapshot operations split recordings into timestamped segments.
  Pool builders and warm members do not record; capture starts at checkout.
- Add `env.recording` status, stop, download and deletion, plus CLI `--record`
  and `sandweave recording` commands. Recording remains off by default.

## 0.2.13

- Use scalable socket readiness checks for synchronous connections with high
  file descriptor numbers. Keep the async transport and bounded executors.
- Reuse snapshots already present on the destination worker even when a shared
  cache is configured; register the additional location without transferring it.
- Add `Pool(target=..., retain_baseline=False)` to reclaim the pool's generated
  snapshots, image files and stopped writable workspaces after closure. Workers
  protect live references and named caches, reject imports during retirement,
  and resume interrupted cleanup. Existing retention remains the default.
- Reuse previously prepared images in private pool storage without downloading
  or building them again. Shared runtime installation files remain available.
- Negotiate retention support before launching on a worker. Failed builders
  and rejected older workers do not leave cleanup waiting for nonexistent files.

## 0.2.12

- Serve controller records from indexed memory. Persist worker observations in
  the background; acknowledge reservations, ownership changes and job results
  only after their durable commit. Existing controller databases remain readable.
- Use async HTTP for controller forwarding, outbound worker bridges and SDK
  command/file operations. Batch relay requests and responses, isolate dashboard
  work, and separate observation work from launches and termination.
- Reuse synchronous connections across calling threads, bound retained idle
  connections, and increase the worker listener backlog for concurrent arrivals.
- Cache completed process status and read streams in larger chunks. Add
  `process.result.aio()` and close async connections when their event loop ends.
  Upgrade clients, controllers and worker processes to apply all changes.

## 0.2.11

- Extend remote ownership's heartbeat grace period from 30 seconds to ten
  minutes on workers and controllers. The SDK continues renewing automatically
  every five seconds, allowing temporary connection loss without early cleanup.
- Keep direct detection of local process exit, explicit termination, detached
  environments and TTL behavior unchanged. Restart older workers and controllers
  after upgrading to apply the longer timeout.

## 0.2.10

- Add `ProxyPolicy(distribution="random", region=None)` to `Network`, with
  random, round-robin, shared-proxy and shared-region assignment. Proxy catalogs
  accept URL lists or mappings from region labels to URL lists.
- Coordinate explicit pool policies across workers. Save each assignment and
  the pool's selection/cursor together, preserving them through retries and
  controller restarts. Standalone sandboxes select one eligible proxy.
- Add `--proxy-policy` and `--proxy-region`, and show region/policy details in
  `env.info["network"]`. Require policy support on participating processes.
- Preserve standalone memory-snapshot bindings and filesystem-cache overrides.
  Explicit pool policies use filesystem baselines; baseline preparation does
  not consume a member's rotation position.

## 0.2.9

- Add `Network(proxy=...)` for one proxy URL or a list. Each sandbox selects
  one proxy for its lifetime; setup and SDK commands receive standard proxy
  environment variables. The external network policy blocks direct egress,
  including direct DNS and UDP.
- Preserve proxy selection through live snapshots. Filesystem-cache restores
  can select another proxy or switch to internet/offline networking. Proxy
  hostnames are resolved on the worker and pinned in the guest hosts file.
- Add `--proxy-file` and credential-free endpoint details in `env.info`.
  Dashboard resource summaries omit proxy credentials.
- Add a reproducible QUEST-RL proxy assessment covering search, source pages,
  browser challenges, repeated requests, and distinct exit addresses.

## 0.2.8

- Add `Memory(disk="16GiB", disk_path="/scratch/my-memory")`. Each sandbox gets
  private disk backing in a random subdirectory of the explicitly selected path.
  The guest sees its RAM allowance plus disk memory; Weave reserves RAM and
  runtime overhead. Kernel memory limits use delegated cgroups or dedicated
  Slurm steps, preserving the worker's CPU selection.
- Preserve disk-backed memory through live snapshots. Restores create fresh
  backing files and can use another disk directory. Termination, failed startup
  and owner-process crashes release the backing storage.
- Add CLI `--disk-memory` and `--disk-path`, template memory settings, and
  backing-directory information in `env.info`. Obtain a compatible engine on
  first use when an existing installation predates disk memory.
- Publish the corresponding runtime binaries and document host requirements.

## 0.2.7

- Add `Pool(shared_cache="/shared/path")` for an explicit immutable baseline and
  image cache. Workers sharing storage reuse its files; transfers to node-local
  caches are coordinated across workers. Writable sandbox state remains separate.
- Reuse verified runtime materialization while file identities remain unchanged,
  avoiding repeated full-image reads from shared storage on later leases.
- Add cluster pool `affinity="machine"` and `affinity="worker"`. Prefer the pool's
  initial location, spill over when needed, and retain preferences across idle
  periods and controller restarts. Machine identity uses the Linux boot ID.
- Include `shared_cache` and `affinity` in pool information and `machine` in worker
  listings. Allow updates to affinity for future assignments.

## 0.2.6

- Allow a live cluster client to acquire new sandboxes after its last worker
  lease expires during an idle period. Keep expired assignments fenced and
  preserve automatic cleanup when the client exits.
- Redistribute CPU time left unused by partially active sandboxes. Account for
  runnable threads and unthrottled consumption when allocating weighted shares;
  preserve explicit quotas and reclaim shares when demand increases.

## 0.2.5

- Make concurrent snapshot imports idempotent after publication. Compare replica
  content independently of its host paths, while retaining checksum checks.
- Propagate failed pool preparation to pending and claiming leases. Preserve the
  original error while the controller finishes cleanup.
- Serialize each sandbox's create, claim and terminate operations. Reject stale
  controller work and compare replay requests independently of dictionary order.
  Schedule launches and termination before routine status polling.
- Add `cluster.remove_worker(worker_id, lost=True)` and CLI `cluster remove
  lab WORKER_ID --lost` for allocations an operator has confirmed stopped.
  Keep ordinary unreachable-worker reservations intact.
- Cap advertised memory budgets at visible cgroup limits, including a narrower
  job step. Slot counts remain independent of memory admission.

## 0.2.4

- Add `Sandbox(image="docker://...")` and CLI `--image` for public Linux x86-64
  registry images. Custom templates can define an image; an explicit constructor
  image overrides that base while retaining setup, services, and controls.
- Preserve image environment, user, and working directory. A private static
  control runtime supports images without Python or a shell. ENTRYPOINT and CMD
  remain metadata; templates define startup services.
- Cache prepared filesystems by image digest. Image bases travel with snapshots
  and pool baselines. Report the image digest in `env.info` and preparation time
  in `env.timings`. Add an image guide to the documentation site.
- Wait for worker file preparation across slow storage and client interruption.
  Start CPU monitoring after controller acknowledgment, and read runsc state
  under its lock to avoid observing incomplete startup records.

## 0.2.3

- New clusters accept direct HTTP and SSH connections by default. Startup prints
  both addresses and complete worker join commands without transport flags.
  Ports are assigned automatically so multiple clusters can run on one machine.
- Startup prints a reusable dashboard sign-in URL. The browser exchanges its
  credential for a read-only session and removes it from the address bar.
- HTTPS addresses are printed when certificates are configured. Explicit
  loopback listeners retain their restriction and label their URLs as local.

## 0.2.2

- Setup streams guest images and desktop/VR helpers to the host installer,
  which writes them in the selected storage directory as the host user.
  Guest ownership no longer needs to map to the destination's host group.
- Build output is accepted only after both archives pass checksum verification
  and the guest exits successfully. Interrupted transfers publish no output.
- Release validation exercises the installed transfer path with private,
  inherited-group and default-ACL directories where available.

## 0.2.1

- Storage settings are local to each project. Setup no longer reads the old
  home-directory location setting, discovers runtimes in parent directories,
  or imports another installation's runtime and cluster settings when choosing
  a new directory. Existing installations remain available through an explicit
  `SANDWEAVE_HOME` or setup selection.
- Cluster startup prints complete join and dashboard commands. `--transport`
  selects SSH, HTTP or HTTPS; HTTP(S) join links include authentication.
  `cluster instructions` prints the commands again. Worker join checks the
  connection before preparing runtime files. `cluster start --json` retains
  machine-readable status output.

## 0.2.0

- `sandweave dashboard` opens a read-only cluster dashboard with live resource
  measurements, retained charts, worker/GPU/workload views, events, and bounded
  logs. It uses the controller's HTTP, HTTPS, or SSH connection. Authenticated
  Prometheus metrics are available at `/metrics`.

- Weave adds named clusters over existing local, SSH and Slurm workers.
  `Sandbox(target="lab")` and `Pool(target="lab")` use durable placement,
  weighted scheduling, ready reserves and worker draining.
- `Job.submit(...)` stores commands, inputs, attempts and results, with explicit
  retries, cancellation, batches and repeat intervals. Controllers can restart
  without restarting active commands.
- Workers enforce assignment generations and sandbox-specific credentials.
  Verified snapshots can transfer between worker storage directories.
- First-use cluster setup retains the selected storage directory and prepares
  missing templates on workers. Idle control connections reconnect before a new
  request; mutations with uncertain delivery still report that uncertainty.

## 0.1.2

- `env.run(...)` and `await env.run.aio(...)` now default to `check=False`.
  Failed commands return their stdout, stderr and exit code. Pass `check=True`
  to keep raising `CommandError`. Timeouts and connection failures still raise.
- `sandweave.__version__` reads the installed package version.
- Maintainers can validate and publish an SDK release with `./deploy`.

## 0.1.1

- Setup downloads compatible, verified runtime binaries from GitHub Releases
  and falls back to building from source when needed.

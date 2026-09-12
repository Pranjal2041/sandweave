# Changelog

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

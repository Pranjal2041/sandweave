# Harbor adapter

Sandweave 0.2.20rc5 integrates Harbor 0.23.0 through its dataset, task,
environment and trial protocols. It has no dispatch rules based on benchmark
names. This replaces the prebuilt-image restriction in rc4.

## Client contract

```python
from sandweave import Benchmark

bench = Benchmark("harbor", source="terminal-bench/terminal-bench@3.0.0", capacity=8)
task = bench.next()
try:
    env, instruction = task.env, task.instruction
    # Run the client's agent.
    result = task.evaluate()
finally:
    task.close()
    bench.close()
```

`source` accepts Harbor dataset names, `DatasetConfig`, `TaskConfig`, dataset
configuration dictionaries, repositories, local tasks and lists of sources.
Harbor resolves registries, package digests, Git revisions and filters. The
resolved native task configuration is retained for `Trial`; discovery does not
replace registry provenance with a local-path-only task.

Sandweave suspends the original `Trial` at its agent boundary and resumes it
when the caller requests evaluation. Harbor owns setup, timeouts, phase network
policies, artifacts, verifier invocation, rewards, step progression and result
files. Task and verifier sources are unchanged. Native named rewards appear in
`result.rewards`; the adapter does not invent a pass threshold.

Multi-step tasks retain their sandbox. Call `task.evaluate()` and then
`task.next_step()` for each phase. Harbor's aggregation and early-stop rules
remain in effect. Final evaluation stops the trial; `task.close()` returns its
capacity. MCP definitions, skills location and the native agent result context
are available through `task.env.harbor`. The caller still implements its agent.
Harbor's own agents can use the environment provider through its CLI.

## Structure and execution

- `benchmarks/harbor/__init__.py` delegates source resolution to Harbor.
- `runner.py` supplies the pull agent, suspended trials and task capacity.
- `provider.py` implements Harbor's environment and per-service operations.
- `compose.py` resolves Harbor's Compose files and overlays with Docker's parser,
  then creates native service groups. No Docker daemon runs task workloads.
- `templates/build.py` builds original Dockerfiles using an isolated BuildKit
  sandbox and imports the OCI result into a portable Sandweave filesystem base.
- `sandbox/services.py` owns group placement, admission, private networking,
  volumes and cleanup. Each service has a separate gVisor sandbox.

Builds accept local/remote contexts, inline Dockerfiles, arguments, stages,
additional contexts, secret mounts and network settings. Registry authentication
uses Docker configuration and credential helpers. Remote workers need credentials
for direct private image pulls.

Services preserve task-defined commands, entrypoints, working directories,
users, environment, health checks, dependency ordering, restart policy and stop
signals. Private networks resolve service names and aliases. Named volumes,
bind inputs, secrets/configs, tmpfs and read-only roots are configured before
starting services. Bind inputs are copied; client filesystem changes are not
live host mounts. Post-stop artifact transfers remain available.

Service volumes carry guest UID/GID metadata independently of the host user's
UID. This engine feature is opt-in for SDK-owned volumes; ordinary host mounts
retain their semantics. The backing filesystem must provide statx inode birth
times. Volumes and ownership metadata are removed with their service group.
The guest mount helper is freestanding and has no libc dependency.

## Capacity and cleanup

One capacity budget and ready reserve cover the benchmark's task attempts.
Single-image environments use lazy filesystem pools with no independent warm
reserve. Image resolution and matching builds share futures. Inputs and tests
are copied after checkout. Separate verifier environments and sidecars consume
normal worker resources in addition to the task-attempt budget.

Launch waits have their own bounded executor. They cannot occupy command/file
transfer executors. Cancellation drains Harbor's shielded environment cleanup
before capacity is returned. Failed group startup attempts cleanup for every
member; reservations remain charged until their runtimes stop. Shutdown drains
outstanding builds and closes their connections.

The agent clock starts after checkout, not while waiting in a warm reserve.
Harbor's reserved grading directories are cleared between shared-verifier steps
before the next agent phase. Task inputs and grading commands are not rewritten.

## Acceptance on 2026-09-16

Disposable tests used allocation 10450695 on `babel-u9-24`, without host sudo or
KVM. User desktops were not used.

| Original upstream task | Reference solution | Fresh unsolved attempt |
| --- | --- | --- |
| TB3 `terminal-bench/interleaved-vigenere` | reward 1 | reward 0 |
| QuixBugs `quixbugs-python-detect_cycle` | reward 1 | reward 0 |
| Harbor `examples/tasks/sidecar-artifacts` | reward 1 | reward 0 |

These ran through public `Benchmark`, including original Dockerfile builds,
separate verifiers and sidecar collect hooks. The authored acceptance runner is
`scripts/accept-harbor-benchmark.py`. Earlier rc4 acceptance also ran the original
TB2 `openssl-selfsigned-cert` through both `Benchmark` and Harbor's CLI oracle.

Regression coverage includes:

- Local and HTTP multi-worker task pulls, capacity, cancellation, distinct image
  reuse, separate verifiers, native rewards, phase environment, multi-step state,
  file modes/symlinks/empty directories and output larger than 1 MiB.
- Compose DNS/HTTP, dependency health, initialized shared volumes, non-root
  ownership and denial to other UIDs, hard links, configs, secrets, read-only
  inputs and root, tmpfs, hostname, extra hosts and sidecar artifacts.
- Restart after failure, resource limits, supplementary groups, graceful custom
  stop signals, and cleanup of detached/double-forked descendants. Failed health
  checks release every member and its reservations.
- Remote Git Dockerfile builds and inline builds with additional context, build
  arguments, network disabled and a secret absent from the output filesystem.
- Public/offline/IPv4 allowlist transitions, hostname/CNAME/wildcard resolution,
  expiration and bounded DNS stream/caches. A real TLS registry exercises Basic
  authentication, Docker credentials, blob transfer and digest verification.
- MCP stdio communication, skill files and native agent result context.
- Existing SDK, terminal, snapshots, OSWorld adapter and benchmark-pull tests.
  Eight simultaneous leases, cancelled waiters and unrelated commands are covered.

The host suite passed 529 tests with five skips. Engine metadata/config tests
passed. A clean build from the pinned upstream source plus the committed patch
produced runtime `2026.09.16.1`. A fresh installed wheel outside the checkout
passed automatic installation, exact engine hashes, no compiler, CPU settings,
network policies, pause/resume, filesystem snapshots, live process-memory
restore and cleanup. First installation took 74.9 seconds; the subsequent coding
sandbox reported 1.10 seconds to readiness. These are observations on this host,
not cross-host performance guarantees.

All five service lifecycle tests and four OSWorld runtime regressions also passed
against the fresh installation. The original sidecar oracle was repeated there:
reward 1, with the helper read from inside the guest matching release SHA-256
`fdff7df37bf7b1f026c06b97fd55b5dc6d428825a995c018cdb8d5097fe71afd`.
This replaced an earlier service run that had selected an older local feature
engine; that earlier run did not qualify the final helper. A further 30 targeted
coordination tests passed after reviewing GPU alternative matching and ensuring
inventory reuses collected runtime status instead of reading it twice.

The combined Harbor/protocol/acquisition run passed 69 checks and skipped one
optional task selection, but exposed an unordered local-pool wakeup. After
adding arrival-order capacity reservation, all four live acquisition tests and
41 targeted host tests passed. The regression includes 16 ordered waiting
callers; launches remain concurrent once capacity is reserved.

Docs were built strictly, their examples and links checked, and their navigation
and copy controls exercised in Chromium. Harbor desktop/mobile renders were
opened and inspected.

An earlier broad run encountered a Git/OpenSSL TLS bad-record-MAC while BuildKit
fetched GitHub. The unchanged remote-context test passed on rerun. Its cause was
not isolated; the failed log is retained, not counted as a successful check.

Detailed local logs and original-task reports are under
`/scratch/pranjala/sw-harbor-20260916`. Trial results remain under that test
installation's `data/benchmarks/harbor/trials`. Release receipts are retained
under `runs/deploy`. These paths are evidence, not required installation inputs.

## Runtime and agent boundaries

The environment supports Linux amd64; an image cannot supply a missing kernel
feature, Windows kernel, TPU or host device. One GPU per sandbox is supported,
including a list of acceptable GPU models. CPU LIMIT uses quota; memory has a
hard ceiling plus separately admitted runtime overhead. Storage quotas and
request-only memory bursting are not advertised.

Compose host namespaces/devices and external host services are not equivalent
to sandbox-local services. The adapter does not claim every Docker Compose
extension or privileged workload is portable. IPv6 allowlists are not supported.
Hostname policies allow resolved IPs, not HTTP Host/TLS SNI enforcement.

MCP clients, model calls, agent resume/trajectory support and simulated-user
bridge protocols belong to the selected agent. The pull agent exposes task
inputs; it does not pretend to implement an ACP agent. Native Harbor agents
retain their capabilities when run with the Sandweave environment provider.
Upgrade clients, controllers and workers together for the added worker RPCs.

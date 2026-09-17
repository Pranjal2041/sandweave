# Harbor contract review — 2026-09-16

The unit of compatibility is Harbor's task/environment/trial contract, including
interactions between its features. A dataset is an acceptance example, not the
specification. Writing reusable functions or avoiding dataset-name conditionals
does not prevent overfitting if one dataset determines implementation and testing.

This review used the installed, pinned Harbor 0.23.0 source: `environments/base.py`,
`capabilities.py`, `resource_policies.py`, `definition.py`, the Docker provider and
its Compose overlays, task/trial configuration models, task loading, agent
capabilities, the original Trial, single-step/multi-step runners, artifact handler
and verifier. It also traced Sandweave's source loader, suspended trials, provider,
Compose translation, service groups, image build/import and command/file APIs.

## Requirements and ownership

| Contract | Implementation and evidence |
| --- | --- |
| Local task, local dataset, native TaskConfig/DatasetConfig, registry, package, repository; versions, filters and download settings | Harbor's DatasetConfig and TaskClient resolve sources. Native source configurations, content digests and Git revisions survive discovery. `test_harbor.py` covers local discovery and duplicate task names; earlier registry/package acceptance is recorded in `harbor-adapter.md`. |
| Task metadata, instructions, extra instructions, canaries, configuration migration | Original Harbor Task and Trial perform these operations. Discovery reads metadata; Trial validates verifier inputs using the actual trial configuration. |
| Prebuilt image, Dockerfile, Compose and additional overlays | Harbor definition selection and its Compose parser/overlays; BuildKit builds original recipes. Plain and Compose service image tags use the same benchmark-wide pinning futures; service definitions contain the selected digest. Existing live tests cover multistage, inline, remote Git, additional contexts, secrets and build reuse. |
| CPU and memory policies, overrides, image user/environment/workdir | BaseEnvironment resolves policies; provider maps CPU limits and guest memory. Compose applies upstream resource defaults before task overrides. Resource-mode host tests and live rendering checks verify order. Guest memory still has a finite Sandweave ceiling. |
| Main exec: shell, user, cwd, persistent/command/scoped environment, callbacks, timeout, complete streams | Provider uses Bash and Harbor defaults, and returns the native ExecResult. Persistent < command < scoped environment precedence also applies through the pull handle. Transfer/output live checks include streams over 1 MiB. |
| Sidecar exec and artifact operations | Per-service views use POSIX sh and their own image defaults. Main agent scope/user/cwd do not leak into them. The minimal-sidecar test uses an image without Bash, a collect hook and artifact export. |
| Transfers, path helpers, ownership, modes, links, empty directories, filtered/excluded logs | Archive transfer plus inherited Harbor helpers. Existing live tests cover transfer metadata, complete output and sidecar exclusions after the service exits. |
| Environment/step setup and health, logs, skills, MCP inputs | Original Trial performs setup and health checks. MCP servers, skills path and native result context are exposed to the client's agent. Existing live MCP test exchanges a stdio message. |
| Native/ATIF prior trajectory and step continuation | PullAgent forwards the original trajectory path, resume flag and prior native result context. The client owns its model conversation. Live tests cover a prior ATIF file and resumed second phase. |
| Shared and separate verification, verifier environment/invocation/user/env/timeout, collect hooks and artifact manifests | Original Trial and Verifier run these paths. Separate verifier lease capacity is independent of agent capacity; build/image futures remain shared. Four simultaneous same-image, two-step tasks exercise the overlap with unrelated sandbox work. |
| Multi-step state, setup, per-step policies, thresholds, reward aggregation | Original MultiStepTrial owns progression. Intermediate results retain their native metrics; final result uses native aggregation. A later step exception is checked before aggregate rewards are returned. |
| Verification disabled | Discovery permits absent verifier files. Original Trial skips verification. Evaluation explicitly records skipped=True and no reward. Live checks cover individual/dataset discovery and one/multiple steps. |
| Native agents and the pull client | Both use the same environment provider. Live checks run Harbor's own Oracle agent through native Trial, for single- and multi-step fixtures. The pull interface reserves task/trial selection and does not execute a second model agent. |
| Phase network policy and service topology | Public/offline/IPv4 allowlists use native network policy operations. The existing worker network_policy dispatch already keeps internal/none service networks offline when a later phase becomes public; no duplicate adapter enforcement is needed. Existing live checks cover allowlist and phase transitions. |
| Service dependencies, health, restart and termination | Native groups preserve commands, entrypoints, resource admission and service APIs. Timed-out health probes consume retries instead of aborting startup. Existing lifecycle tests cover restart, groups, limits, custom signals and detached descendants. |
| Service volumes, read-only root/inputs, configs/secrets, tmpfs and image metadata | Native groups own private worker volumes. Existing live tests exercise populated shared volumes, non-root ownership, read-only filesystems, secrets, hostname and extra hosts. Client bind inputs are transferred copies, not host filesystem mounts. |
| Capacity, warm reserve, cancellation, failed startup, shutdown | Task attempts share one capacity budget. Blocking worker operations do not hold the capacity condition. Reservations survive slow teardown. Existing local/HTTP multi-worker tests cover concurrent acquisition, cancellation, failures and group admission; a deterministic check holds one teardown open while releasing another lease. |

## Corrections from this review

The rc7 changes fix sidecar shell/scope leakage, pull-command scoped environment
precedence, missing upstream resource overlays, health-probe timeout handling,
absent optional dependencies, benchmark-wide Compose image pinning, same-image
intermediate verifier deadlock, capacity locking during warm eviction, trajectory
and continuation inputs, disabled-verifier discovery, and hidden multi-step
failures. Command cleanup also preserves the original failure if termination fails.

The authored fixtures combine requirements instead of choosing another benchmark
and patching until that benchmark passes. They use upstream task definitions,
Trial and Verifier; their expected behavior comes from those contracts. No
CyberGym task was run for this review.

## Runtime boundaries

Harbor's provider protocol does not make every provider a complete Docker host.
BaseEnvironment has explicit optional capabilities; its own cloud-provider
contract also permits transferred paths instead of live host binds. Sandweave
currently supplies Linux amd64 guests, up to one GPU per sandbox, CPU limits,
finite guest memory, and IPv4/hostname allowlists. It does not supply Windows,
TPUs, storage quotas, resource-request policies, arbitrary host devices/namespaces,
or IPv6 allowlists.

The Compose translator is not a complete implementation of every Compose
extension: host/service namespace sharing, external volumes/drivers, image
volumes, service-derived build contexts and host-device declarations are not
implemented. Some upstream CLI actions (install-only, regrade/source-trial) have
no agent lease and belong to Harbor's CLI rather than Benchmark.next(). These
boundaries must not be presented as universally compatible Harbor workloads.
Neither source review nor a passing suite proves arbitrary future task code will
run on a different kernel implementation.

## Validation

Before packaging, the host suite passed 570 tests (four optional skips), and the
new contract suite passed 12 live checks. The tests are in
`tests/test_harbor_contract.py` and `tests/integration/test_harbor_contract_live.py`.
`scripts/deploy.py` includes both in its installed-wheel acceptance alongside the
existing Harbor, build, network, lifecycle and core SDK checks. Release acceptance
must complete before publication; the receipt records the exact commit and both
distribution hashes. Review then removed one redundant internal-network adapter
check and its mock test: the worker already enforces that constraint. The final
host suite therefore contains 569 tests plus the same four optional skips.

Local evidence for this review is under
`/scratch/pranjala/sw-harbor-contract-20260916`; durable test implementations and
this record are committed in the repository. Engine source and runtime pins did
not change during this review.

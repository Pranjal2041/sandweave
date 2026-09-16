# Harbor implementation review

The rc4 adapter incorrectly used the legacy registry and assumed tasks supplied
prebuilt images. That excluded TB3's package source and Dockerfile-based tasks.
The rc5 implementation follows Harbor 0.23's source and environment contracts.
See [harbor-adapter.md](harbor-adapter.md) for API, acceptance and boundaries.

## Contract mapping

| Harbor responsibility | Implementation |
| --- | --- |
| Dataset/package/Git/local discovery | Native `DatasetConfig`, `TaskConfig`, `TaskClient` |
| Source revisions and task filters | Native configs retained in `TrialConfig`; resolved Git/package revisions pinned |
| Setup, steps, timeout, verifier, rewards, artifacts | Original `Trial`, paused at its agent hook |
| Environment image/build/service definition | Native definition selection and Docker Compose merge/parser |
| Dockerfiles | BuildKit build, OCI export, portable Sandweave import |
| Service execution | Direct native sandbox group, shared placement and admission |
| Per-service exec/copy/stop | Harbor provider forwarding to the requested service |
| Mounts, ownership and read-only root | Private service volumes and opt-in engine ownership metadata |
| Phase networking | Runtime public/offline/IPv4 allowlist policy updates |
| CPU/memory/GPU settings | Quota, admitted guest/runtime memory, acceptable GPU models |
| MCP/skills/results | Original configs and native result context exposed to the client |
| Cancellation and shutdown | Drain trial, environment, build and lease cleanup before closing connections |

No implementation branch selects behavior by TB2, TB3, QuixBugs or another
dataset name. Original tasks remain test inputs rather than implementation rules.

## Corrections found during acceptance

- Snapshot restoration discarded image ENV when command environment overrides
  were present. Overrides now extend the image environment.
- Non-root task users could not write verifier logs. Writable Harbor paths now
  use Harbor's directory and permission helper.
- CPU AUTO/LIMIT previously advertised vCPUs without a quota. It now enforces
  the requested ceiling. Memory request-only behavior is not advertised.
- Stale runtime PIDs could refer to unrelated host processes. Runtime status
  checks verified process identity before treating those PIDs as live.
- Group cleanup stopped after one failure and undercounted members. Cleanup now
  attempts all members and retains admission until all runtimes stop.
- Importing a built image briefly creates another runtime. That runtime and
  its memory are reserved with the builder before placement.
- Host UID ownership could not represent arbitrary guest users on shared volumes.
  SDK-owned volumes now carry private inode ownership records across gofers.
- Compose-rendered dollar escaping and string octal modes need interpretation
  before direct execution. Shell dollars and file permissions are preserved.
- Service entrypoint exit must stop detached descendants as well as exec process
  groups. Guest PID 1 reaps them while retaining artifact-transfer processes.
- An extra Compose overlay must not suppress Harbor's prebuilt-image input copy.
  It now follows the same post-start helper as other task definitions.
- Transfer cleanup inherited a not-yet-created task working directory. Setup
  commands now run from `/`, and working directories are created before root
  becomes read-only. Transfer archives use the writable control directory, so
  read-only tasks do not require a writable `/tmp` for artifact collection.
- Trial cancellation shields native cleanup. The provider retains its cleanup
  task and the pull session drains it rather than returning capacity early.
- HTTP scoped worker permissions omitted new service operations. Those operations
  now retain allocation authorization and are covered by live HTTP tests.
- The combined live run exposed unordered local pool wakeups: a later waiter
  could acquire the only released slot while the first caller kept waiting.
  Local checkouts now queue by arrival and reserve available capacity before
  leaving the queue. Launch and setup still run concurrently outside the lock.
  Timed-out and cancelled waiters leave the queue and wake their successors.

## Evidence and scope

Acceptance includes original TB3, QuixBugs and Harbor sidecar tasks, each with
both an original reference solution and a pristine negative control. Tests also
cover native multi-step and separate-verifier behavior, network transitions,
MCP/skills, cancellation, concurrent pulls, service failure, remote and inline
build contexts, secret mounts, authenticated TLS registry transfers and cleanup.

The engine was rebuilt from clean upstream sources plus the committed patch.
Its helper is freestanding; no task image utility or libc version is assumed.
Fresh installed-wheel acceptance covers automatic first use and existing
filesystem/process-memory snapshots without compiling the engine.

This is a Linux sandbox provider, not a replacement Windows kernel or an
implementation of every host feature in Docker Compose. Agent-specific model,
ACP, resume and trajectory behavior stays with the chosen agent. These are
explicit capability boundaries, not dataset-specific fallbacks or modified
verifiers. The acceptance record identifies what was actually exercised.

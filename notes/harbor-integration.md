# Harbor integration: saved design

Recorded 2026-09-11. This is an agreed direction for future work, not a shipped
integration. The user is taking a detour; do not start implementation merely
because this note exists. Sandweave 0.2.6 has no Harbor provider or benchmark CLI.

## User intent and decisions

Make it possible to select a Harbor task or benchmark, prepare its environments,
and run them across Weave workers with one simple command. Keep the existing
task instructions, agent implementations and grading scripts intact wherever
their requirements are supported. Users should not need to write a launcher or
maintain duplicate task definitions.

Use direct Sandweave sandboxes. The user explicitly rejected running Harbor and
its Docker backend inside an outer Docker-enabled sandbox for this integration.
This decision does not remove the existing Docker template for other uses.
Retain operation without host sudo or KVM.

Templates own differences between benchmark environments. Some benchmarks will
need only configuration; others will need code. When code is specific to a
benchmark, keep it with that benchmark's template and related scripts/modules.
Do not scatter benchmark-name checks through Sandbox, workers or Weave. A
template is a recipe with optional supporting code, not necessarily only a TOML
file. Do not require code when existing settings already express the behavior.

Share the Harbor lifecycle integration across benchmarks. Add generic runtime
or SDK functionality in its existing module when it is generally useful;
benchmark-specific behavior stays with the template. The exact extension hook
and package layout remain to be designed, rather than promised as existing API.

Retain the project's principles: simplicity, scalability, modularity and
generality. A new benchmark should require as little new machinery as possible.

## How the pieces fit

Harbor owns task discovery, agent execution, verification, rewards and trial
results. Its environment provider delegates start/stop, command execution and
file transfers to Sandweave. Weave owns worker placement, admission, ready pools
and sandbox cleanup.

Use common Harbor template defaults, then read each task's image and settings
from its existing Harbor definition. Add a benchmark template where there is
additional shared behavior. A benchmark can contain many different task images;
there is no assumption of one Terminal-Bench image for the whole dataset.

Sandweave already supports a template-level command shell:

```toml
command_shell = "/bin/bash"
```

The worker uses it for command strings unless a call explicitly supplies a
shell. The coding template's `/bin/sh` is a default, not a runtime restriction.
Templates also carry image, workdir, user, environment, resources, setup,
services and controls. Image and template can be supplied together. See the
[resolver](../src/sandweave/templates/resolve.py),
[command execution](../src/sandweave/sandbox/worker.py) and
[template guide](../docs/templates.md).

| Harbor operation | Intended Sandweave connection |
| --- | --- |
| Start an environment | Create from the task image/template or acquire a prepared pool member. |
| Execute a command | Preserve shell, user, cwd, environment, output streams, exit code and timeout. |
| Upload tests or inputs | Transfer files/directories at Harbor's requested phase and path. |
| Collect rewards and logs | Download them before releasing the sandbox. |
| Stop or cancel a trial | End commands and dispose of the owned sandbox/lease with the requested retention behavior. |

Directory transfer semantics, permissions and symlinks need attention: current
Sandweave recursive uploads/downloads reject symlinks. The provider must also
honor Harbor's scoped user/environment defaults and streaming callbacks where
used. Multi-step trials retain their environment between steps. Separate
verifier environments require their own sandbox and artifact transfer. Keep
test injection and grading in Harbor's lifecycle; do not bake later-phase tests
into the environment presented to the agent.

## Preloading and the user experience

Reuse prepared images and filesystem baselines, and keep a bounded reserve of
ready sandboxes where useful. Image caching avoids repeated download/import;
a warm pool additionally avoids launch latency. These are separate costs.

Pool identities must reflect the resolved image and relevant template/task
settings so incompatible tasks cannot share a baseline. Pin image digests for
reproducibility. Each trial receives pristine writable state; used sandboxes
are discarded and replacements come from the baseline.

A benchmark run can own the necessary collection of pools internally. Do not
make users construct one pool per task, or multiply an unrestricted warm count
by every task in the dataset. Preparation, transfers and ready reserves need
bounded concurrency and resource budgets. The existing Pool APIs provide much
of this machinery; coordinating them for Harbor remains integration work.

No benchmark command spelling, Python constructor, short provider alias or new
term such as "a weave of benchmarks" has been agreed. Harbor already accepts
a custom provider import path through `--env module.path:ClassName` and provider
kwargs through `--ek key=value`. A future wrapper can simplify benchmark
preparation and execution. Do not document a proposed command as runnable.

## Source findings and compatibility boundaries

The investigation inspected Harbor 0.22.0 from its PyPI source distribution
(Python >=3.12), Harbor main at
`eeab9f0843e6af3fea2488b308b0098d8474ca98`, and Sandweave 0.2.6. Harbor supports
external environment providers without a source fork. Its Linux Docker provider
uses `bash -c`; that difference can already be expressed in our templates.

Harbor also ships a [Singularity/Apptainer provider](https://github.com/harbor-framework/harbor/tree/eeab9f0843e6af3fea2488b308b0098d8474ca98/src/harbor/environments/singularity).
It launches its own containers and is not an integration with Sandweave or Weave.

All 89 task definitions in the inspected Terminal-Bench 2 revision
`2fd12b88aafdd04a52c298e3940bcb189f9766d6` declare a prebuilt image and have a
Dockerfile; none defines `environment/docker-compose.yaml`. The ordinary
prebuilt-image path therefore need not rebuild those Dockerfiles. This is a
definition audit, not a check that all referenced images are downloadable or
that all 89 workloads execute successfully. No Harbor trial was run.

- **Images and builds:** Sandweave imports public Linux x86-64 registry images.
  Arbitrary Dockerfile builds, private registry authentication, other CPU
  architectures and Windows containers are not supplied by this image API.
  Dockerfile-only tasks and Harbor's force-build behavior need a build path.
  Do not translate arbitrary Dockerfiles into shell scripts and assume parity.
- **Startup:** Image ENV, USER and WORKDIR are supported; ENTRYPOINT and CMD
  are metadata and do not run automatically. Preserve the selected Harbor
  provider/task startup behavior through the template's services where needed.
- **Multiple services:** Compose tasks require networking and per-service
  exec/copy/stop behavior. Describing services in a template does not by itself
  implement those semantics. The rejected nested-Docker approach is not the plan.
- **Resources:** Preserve Harbor's requested policy. Sandweave `vcpus=1` is
  not Docker's one-core quota. Sampled CPU limits are not hard host cgroup
  limits, and guest memory and runtime memory are separate budgets. Storage
  requirements also need an explicit mapping. Do not silently equate different
  enforcement mechanisms or promise benchmark timing equivalence.
- **Networking:** Sandweave supports public/offline guest networking, but lacks
  Harbor's richer allowlists and dynamic policy changes between phases. Client
  connectivity is separate from guest networking; generic guest TCP forwarding
  is not provided by the cluster command/file relay.
- **Runtime support:** A Linux image cannot supply missing kernel features or
  host devices. Such work may require general runtime changes, not only a
  benchmark template. Cached startup does not imply faster task execution.

The next implementation round needs complete trial acceptance: unchanged tasks,
known-solution verification, negative grading, artifact collection, cancellation
and cleanup, concurrent remote workers, cold preparation and warm reuse. Check
resource/network semantics as well as successful commands. Expand benchmark
coverage from evidence; do not label image acceptance full harness acceptance.

## Sources and resumption references

- [Harbor concepts](https://www.harborframework.com/docs/core-concepts) and
  [task format](https://www.harborframework.com/docs/tasks).
- [Harbor 0.22.0 distribution](https://pypi.org/project/harbor/0.22.0/).
- Pinned Harbor [base environment](https://github.com/harbor-framework/harbor/blob/eeab9f0843e6af3fea2488b308b0098d8474ca98/src/harbor/environments/base.py),
  [provider factory](https://github.com/harbor-framework/harbor/blob/eeab9f0843e6af3fea2488b308b0098d8474ca98/src/harbor/environments/factory.py),
  [CLI](https://github.com/harbor-framework/harbor/blob/eeab9f0843e6af3fea2488b308b0098d8474ca98/src/harbor/cli/jobs.py),
  and [Modal provider](https://github.com/harbor-framework/harbor/blob/eeab9f0843e6af3fea2488b308b0098d8474ca98/src/harbor/environments/modal.py).
- [Inspected Terminal-Bench 2 revision](https://github.com/harbor-framework/terminal-bench-2/tree/2fd12b88aafdd04a52c298e3940bcb189f9766d6).
- [Existing image acceptance](oci-images.md), [Weave usage](weave-usage.md),
  [CPU sharing](cpu-demand-sharing.md), and [process ownership](process-ownership.md).
- [gVisor compatibility](https://gvisor.dev/docs/user_guide/compatibility/).

Temporary source copies used during research are under
`/tmp/sandweave-harbor-review-release-0220`,
`/tmp/sandweave-harbor-review-eeab9f0`, and
`/tmp/sandweave-harbor-terminal-bench-2-2fd12b8`. They are disposable reference
copies, not required runtime inputs or durable evidence; use the pinned upstream
revisions above to recover them. No runtime or public API changes were made.

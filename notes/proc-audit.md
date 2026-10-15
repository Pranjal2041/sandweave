# Optional procfs visibility and runtime audit

Sandweave 0.2.42 introduces independent `hidepid` and `audit` creation options.
Both are off in existing templates. The engine adds per-mount hidepid permission
checks and an opt-in SecCheck sink; the SDK passes settings through ordinary
sandbox, pool, and snapshot definitions. See the public
[contract](../docs/process-audit.md) for coverage and limitations.

## Engine behavior

The procfs mount parser accepts hidepid 0/1/2. Lookup, directory enumeration,
permission checks and cached-path stat use the caller's filesystem credentials
and ptrace read checks. The inode stores its filesystem's policy, so live
checkpoints retain it. Unprivileged agents must not have a route to root.

The audit sink receives a host-opened, exclusive-create FD. Its JSONL writer
runs asynchronously with a bounded queue, a configurable total byte limit, and
sticky error/loss reporting. A status request inserts a queue barrier, flushes
and fsyncs its prefix, and returns the sequence and byte boundary. Client
exports read only that prefix in bounded chunks. Export status never means an
episode is free of tampering: it establishes collection completeness for the
listed points. Guest root cannot access the host FD through guest procfs.

A required initialization failure prevents boot. Disabled tracing retains the
existing path. Memory captures are rejected in audit mode; filesystem restores
start independent streams. Audit records are outside snapshots. No Linux Audit
netlink provider, auditd rules engine, or automatic tamper classifier is claimed.

## Acceptance

Test host: Babel u9-24, Linux 5.14.0-687.25.1.el9_8.x86_64, existing Slurm
allocation 10689224. No host sudo, KVM, GPU, or user desktop was used. All live
environments and workers were private to this acceptance.

- Engine procfs, audit sink and trace command suites passed. Audit tests cover
  concurrent ordering, bounded-queue loss, log limits, existing-file/symlink
  refusal, and actual write failure through a read-only donated FD.
- Host suite: 812 tests passed, 8 skipped; one subprocess-import test needed an
  absolute PYTHONPATH on rerun. The new coverage-manifest test also passed.
- Public SDK tests passed for hidepid 0/1/2, root-warmed cached paths, cross-user
  files and thread directories, same-user processes, and privileged access.
  A memory snapshot retained hidepid=2.
- With 16 concurrent client threads, 48 commands across audited and ordinary
  sandboxes completed. One verified run exported 29,821 ordered events with
  zero drops. Filesystem restore retained both flags; forced log exhaustion
  reported incomplete coverage while commands remained usable.
- Two-worker Weave acceptance passed for controller forwarding, direct borrowed
  handles, async export, and two concurrent pool members.
- A GNOME sandbox with both flags reached its Xvnc desktop and exported 778,653
  events (507,020,566 bytes), zero drops. Its actual 1920x1080 screenshot was
  opened and inspected. This demonstrates substantial audit volume on a busy
  desktop; it is not a performance guarantee or a tamper-classifier result.
- The packaged clean runtime passed fresh-wheel installation, command/file
  operations, memory restore, offline networking, cleanup, and preservation of
  the caller's global installation location. No compiler was downloaded or run
  by the fresh runtime installation.
- Documentation built strictly; browser navigation and copy buttons passed.
  The process-audit page was opened and visually checked.

Private raw event logs may contain command credentials and remain outside Git
under `/scratch/pranjala/sandweave-audit-20261014`. Only summarized acceptance
metadata is public. Reproduce with the gated `test_proc_audit*_live.py` tests
and `scripts/accept-runtime-release.py` against the published runtime.

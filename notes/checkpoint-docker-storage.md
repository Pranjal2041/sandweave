# Checkpoint and Docker storage repair — 2026-09-17

Filesystem capture rejected a directory bind-mounted onto itself because the
mount inventory treated every writable mount as separate storage. The failure
reproduced in an ordinary coding sandbox with `mount --bind /workspace/self
/workspace/self`; Docker was not needed to trigger it.

The inventory now compares a mount's filesystem identity and root against its
parent at the same path. A self-bind adds a view of files already covered by the
backing filesystem export. It adds no archive, extra data copy or checksum pass.
A different filesystem or source directory still cannot be silently omitted.
Repeated self-binds, nested paths, escaped names and unordered mount records
have contract tests. Cold restore recreates the launch mount layout; it does
not reproduce application-created self-bind propagation settings. Docker
reestablishes its own propagation settings when its daemon starts.

The SDK also ignored `runtime_options.docker_data` unless `docker_archive` was
set, while the launcher required a default archive even for empty storage.
Storage selection now works independently of archive import. The built-in
Docker template enables the two checkpointed tmpfs filesystems at
`/var/lib/docker` and `/var/lib/containerd`. An archive remains an explicit
first-launch input. Saved filesystems and memory checkpoints retain the data;
restore does not reopen the archive or replace saved changes with its seed.

These mounts are required for the stock template's overlay2 driver. A deliberate
test with them disabled confirmed Docker's existing `failed to mount overlay:
invalid argument` failure on an overlay-backed upper. An explicit custom
template using Docker's vfs driver instead passed with `docker_data=False`,
including a cold restore. There is no automatic fallback to that slower driver.

## Acceptance

Before packaging, the host suite passed 593 tests with three optional skips.
The existing filesystem-capture script's seven tests also passed. Eight new
live cases in `tests/integration/test_checkpoint_storage.py` and
`tests/integration/test_docker_storage_live.py` passed across the targeted runs:

- Self-binds on the root overlay and on separate storage, with repeated cold
  saves/restores and independent source/clone writes.
- Two concurrent captures while a third sandbox runs commands. Its 23 commands
  took at most 52 ms in this run; this is a measurement, not a latency guarantee.
- Empty Docker storage and a custom vfs configuration without separate mounts.
- A real pulled image, container writable layer, named volume, containerd
  namespace and containerd file through filesystem and memory checkpoints.
  Memory restore retained the running container. Both paths then passed a
  second filesystem checkpoint and restore.
- An explicit archive import, followed by changes, capture, deletion of both
  the source and staged archive, and restore of the saved changes.

The release acceptance command includes these live tests and the existing SDK
snapshot tests against the installed wheel outside the checkout. Its validation
receipt records the exact packaged commit and artifact hashes. Local diagnostic
evidence is under `/scratch/pranjala/sw-checkpoint-docker-20260917`.
No gVisor engine source or runtime binary changed.

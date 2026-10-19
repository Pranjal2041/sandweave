# mount_setattr reference-leak report

Investigated on 2026-10-19 against SDK 0.2.43 and runtime 2026.10.18.1,
engine commit `6bf5da1bb2e26a145952e64bd406f4a257711b23`. The client screenshot
said syscall 442 resolves a mount and leaks its reference when returning
`ENOSYS`. Its reproducer location was truncated, and the client's exact runtime
version was not provided.

That failure path does not exist in this released source. The AMD64 syscall
table in `pkg/sentry/syscalls/linux/linux64.go` has no entry for 442. Its
`Missing` handler emits a rate-limited unsupported-syscall event and returns
`ENOSYS`. Neither that handler nor `Kernel.EmitUnimplementedEvent` resolves a
pathname or acquires a mount reference. No runtime implementation was changed
based on this report.

## Live check

`tests/integration/test_mount_setattr_live.py` ran against the installed 0.2.43
wheel and the release's exact engine bytes on Babel u9-24, allocation 10689224.
No host sudo, KVM, GPU or user-facing sandbox was used. Four private sandboxes
ran the syscall probe concurrently; a fifth ran unrelated commands.

Each probing sandbox:

- Called syscall 442 with an absent path, an invalid directory descriptor and
  an unreadable pathname pointer. All returned `ENOSYS`, consistent with
  rejection before argument or path access.
- Mounted 32 temporary tmpfs filesystems. On each, it repeated absolute-path,
  recursive, directory-relative and `AT_EMPTY_PATH` calls 32 times each.
- Closed its directory descriptor and successfully used ordinary `umount2`
  after each round. No lazy detach was used to mask busy mounts.
- Verified the oracle first: an intentionally open directory descriptor made
  ordinary unmount return `EBUSY`.

Result: **16,396 rejected calls, 128 successful ordinary unmounts**, and all
four held-reference controls returned `EBUSY`. The peer's slowest command took
54.9 ms. The integration test passed in 23.35 seconds. These measurements do not
reproduce or rule out a different engine, call sequence or client reproducer.

Verified executable SHA-256 values:

```text
runsc:        6c365f234835029a0bffaa4df52292cf66fdf5292df80cd0d472b9d745a83193
gvisor_sentry:d3df6168521653733fb7b0c9605d86f603596bba9d1feef3f51c04ce8ed0c6e0
```

Raw logs are under the ignored node-local directory
`/scratch/pranjala/sandweave-mount-setattr-20261019`. The first attempt stalled
in the host Apptainer setup probe before starting any guest: a starter thread
was in `autofs_expire_wait`. Only the private test processes were stopped. The
successful run used a scratch working directory and
`APPTAINER_NO_MOUNT=cwd,hostfs,bind-paths` to avoid automatic host binds;
Sandweave's explicit runtime binds remained enabled. No product defaults or
host configuration were changed. Scratch retained more than 15% free.

To repeat with an isolated worker and already prepared release assets:

```bash
SANDWEAVE_INTEGRATION=1 \
SANDWEAVE_HOME=/path/to/private-worker \
SANDWEAVE_LOCAL_DIR=/path/to/private-local \
SANDWEAVE_ASSETS=/path/to/release-assets \
python -m pytest -q -s -o pythonpath=tests tests/integration/test_mount_setattr_live.py
```

The next diagnostic input needed is the client's complete reproducer path and
the exact engine used by the failing sandbox. An SDK upgrade alone does not
identify an already-running sandbox's engine or a memory snapshot's pinned
engine. There is no runtime fix or new PyPI release from this investigation.

## Follow-up: /var/lib and checkpoint reproduction

The second report identified an installation of SDK 0.2.43 on October 18 and
provided the sequence `mount tmpfs -> mount_setattr(AT_RECURSIVE,
MOUNT_ATTR_RDONLY) -> ordinary unmount`, at `/var/lib/probe-rro`. It reported
`ENOSYS`, then `EBUSY`, followed by filesystem-checkpoint rejection of the
remaining writable mount. The actual reproducer script and failing engine
hashes are still unavailable.

The added `test_var_lib_mount_probe_and_filesystem_checkpoint` covers that
location and one syscall invocation with no other target descriptors opened.
It uses dirfd `-1` and a 32-byte mount-attribute structure, matching
[Moby's recursive-read-only detection](https://github.com/moby/moby/blob/v27.4.0/daemon/daemon_linux.go).
This is a direct syscall-sequence test; it does not start Docker itself.

All three cases passed against the same unmodified released binaries:

| Case | Ordinary unmount | Mount left behind | Filesystem save and restore |
| --- | --- | --- | --- |
| Mount only | Succeeded | No | Passed |
| Mount, syscall 442 returns ENOSYS | Succeeded | No | Passed |
| Deliberately leave mount present | Not initially attempted | Yes | Rejected until ordinary unmount; then passed |

The negative control produced the reported `unsupported writable mount` error
for `/var/lib/probe-rro`, and the sandbox remained usable after the failed save.
The positive cases saved a marker and restored it in a new sandbox, with no
probe mount in the restored mount table. Three tests passed in 21.79 seconds;
raw output is `checkpoint-repro.log` in the same ignored scratch directory.

The follow-up establishes a concrete reported failure to investigate, but
does not reconcile its outcome with the released-source implementation or
these live results. A different call sequence, a retained descriptor, or a
different running engine remain possibilities, not established diagnoses.

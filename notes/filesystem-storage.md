# Writable filesystem storage

SDK 0.2.32 defaults fresh sandboxes to disk-backed writable storage. The API is
`Sandbox(storage=Storage(path="/worker/directory"))`; `storage="memory"` selects
the former behavior. `Pool` forwards the same option, and templates can set
`resources.storage`. The CLI exposes `--storage` and `--storage-path`.

The launcher creates a private random directory under the requested parent,
or its own runtime storage. The root overlay uses an anonymous host file.
Persistent tmpfs mounts, including Docker/containerd, use private disk-backed
allocators through gVisor's existing mount hints. They remain plain guest tmpfs,
allowing Docker's overlay2 driver to operate. Volatile tmpfs mounts and explicit
external mounts retain their existing semantics. Host page cache can contain
file contents and is reclaimable; disk storage does not increase guest RAM.
No storage quota or physical-space reservation is introduced.

The goferless EROFS startup path previously skipped backing-file creation. It
now donates those descriptors even without a gofer process. Filesystem restore
imports into an empty disk-backed persistent mount while retaining its allocator
and resource identity. The archive importer already streams Sandweave exports.
Memory snapshots use gVisor's private-memory-file capture/restore; neither kind
of snapshot depends on the source's backing directory. Each clone gets new files.
Cleanup removes only a directory carrying this launcher's ownership marker,
after its runtime stops. Crashed launchers also have worker cleanup on termination.

Old snapshots without a storage declaration restore as memory-backed, using
their pinned engine for memory restores. A filesystem restore can change modes;
a memory restore can change the host directory but cannot change backing mode.
New disk-backed launches require an updated worker/controller and a qualified
engine. Engine feature verification is cached per immutable build in a worker;
an already-selected engine does not incur an additional launch-time override.

## Acceptance

On Babel, without host sudo/KVM and using disposable sandboxes:

- A 128 MiB guest wrote a dense 384 MiB file. Its filesystem and memory snapshots
  each restored concurrently into two independent custom directories after
  terminating the source. Contents, hard links, permissions and binary xattrs
  were preserved; all private backing directories were removed after termination.
- Explicit RAM storage and conversion of a filesystem snapshot to disk passed.
- Four concurrent 384 MiB writes left unrelated `true` calls at 18.35 ms median
  and 42.00 ms maximum (78 samples). This is a small concurrency acceptance test,
  not a fleet-scale performance bound.
- Empty Docker storage, disabling the separate Docker mounts, image/container/
  volume/containerd preservation through filesystem and memory snapshots,
  repeated cold snapshots, and optional archive import passed. Together with
  the preceding storage cases: 9 tests passed in 92.38 seconds.
- An unmodified installed 0.2.31 wheel created filesystem and memory snapshots.
  The current SDK restored both with their original RAM mode and converted the
  filesystem snapshot to disk. This cross-version test passed in 21.41 seconds.
- The engine tmpfs tests include restoring into a private disk allocator and
  rejecting an already-populated destination without replacing its backing file.

Engine source: `09e7ac5` on `fix/disk-backed-storage`. A separate clean-source
build produced runtime `2026.09.26.2`. The installed SDK release's validation
selection and artifact hashes are recorded by `scripts/deploy.py` in its release
receipt. Test logs and captures are local artifacts under
`/scratch/pranjala/sandweave-storage-20260926`; the reusable tests are committed.

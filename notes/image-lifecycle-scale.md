# Local images and lifecycle scaling, 2026-09-28

Sandweave 0.2.40 closes the five remaining gaps from the client patch audit.
The existing async lifecycle and aggregate CPU broker implementations remain.
No gVisor binary change is required.

## Behavior

- `Sandbox.import_image(path, template=None, target=None, timeout=600)` uploads
  a client-local OCI image-layout tar to a worker and returns a filesystem
  snapshot. The same reference works with Sandbox and Pool. Uploads use bounded
  chunks into a private host directory, without bouncing the image through
  guest TCP. Checksums are carried into immutable publication; metadata and
  layer verification still apply. Importer sandboxes are deleted afterward.
- Layer replacement uses a parent/children index. Replacing a leaf does constant
  work; whiteouts visit the affected subtree. Existing symlink, hard-link,
  directory replacement, ownership and metadata semantics remain.
- The guest output reader rechecks pipe readiness after observing process exit.
  Previously, a child writing between the timed-out select and poll could lose
  those bytes. Completion notifications alone did not close that race.
- Worker inventory and ownership cleanup use an active ID index maintained by
  lifecycle writes and rebuilt once at worker restart. Normal listing retains
  historical records.
- `env.delete(wait=True, timeout=300)` and `sandweave delete` persist cleanup
  intent before acknowledging. Workers retry through a bounded four-thread
  deletion executor and resume after restart. Weave journals the request and
  polls cleanup outside its control executor. Reservations are released after
  termination is confirmed, before file removal completes. Pending deletions
  have their own memory index; reconciliation does not scan released history.
  Private bundles, network files, logs, recordings and import staging are removed.
  Published snapshots, shared images and external mounts survive. Small worker
  and controller tombstones prevent identity reuse and late-create resurrection.

The local-image API accepts one Linux amd64 image in an uncompressed OCI tar;
it does not reinterpret Docker-save archives or bare rootfs archives as OCI.

## Validation

Artifacts: `/scratch/pranjala/sandweave-five-gaps-20260928` on Babel u9-24,
within the existing allocation, without sudo or KVM. The storage gate budgeted
10 GiB of additional growth and checked that at least 15% would remain free.

- The source host suite passed 795 tests, with 11 optional-dependency skips and
  integration/GPU tests deselected. Two initial subprocess import failures were
  fixed by supplying the absolute source PYTHONPATH to the runner, not by changing
  product code. Additional deletion cancellation/restart checks passed afterward.
- Deterministic stdout/stderr tests force the child to write and exit between
  select timeout and poll. The original code returned exit 0 and zero output;
  the updated code retains all bytes.
- Inventory tests keep 1,000 historical records while 50 sandbox identities
  change state concurrently. Health polling reads only active records; a worker
  restart reconstructs the index. Deletion tests cover repeated acknowledgements,
  bounded queues, parallel slow cleanup, worker/controller restart, capacity
  release, async callers and cancellation before worker creation.
- Live local acceptance imported BusyBox from an OCI archive, preserved its
  nonroot UID, working directory and environment, and checked all stdout/stderr
  from 256 commands at concurrency 32. Deletion continued after the client
  handle closed while a peer stayed responsive. Private directories disappeared;
  a saved snapshot still verified and restored its file contents.
- Live Weave acceptance uploaded the archive through outbound relay, removed
  the client's original file, drained the importing worker, and used the saved
  image in a pool on the second worker. Controller-owned deletion completed
  after the handle closed, preserving the snapshot. The final run passed in
  28.67 seconds.
- Native Apptainer deletion passed, including an external bind mount whose
  contents survived. The native memory guard exits cleanly after deletion.
- Documentation passed a strict build and browser checks: 23 pages, 117 Python
  examples and 1,913 internal links. The import/deletion sections were opened
  on desktop and mobile; copy buttons and screenshots were checked.

## Layer replacement measurement

The same metadata-only fixture used a 10,000-file base and replaced 1,000 files:

| Implementation | Replacement time |
| --- | ---: |
| 0.2.39 | 1.172 s |
| 0.2.40 | 0.031 s |

This is a 37.7x speedup for the measured replacement pass, not a claim about
end-to-end download, image packing, or sandbox startup speed.

Release validation separately installs the committed wheel and repeats the
selected regression and live suites. Its receipt is under `runs/deploy/0.2.40`.

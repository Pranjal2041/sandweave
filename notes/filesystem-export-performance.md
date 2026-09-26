# Filesystem export performance

Sandweave 0.2.36 uses runtime 2026.09.26.5 for new launches. The filesystem
exporter now combines tar headers, padding and file data into 1 MiB writes,
and reuses one 1 MiB file-copy buffer across the archive. Previously every
header and padding fragment reached the host file separately, and each file
allocated a new copy buffer. On the small-file baseline, `os.File.Write`
accounted for 53% of sampled sentry CPU time.

The change applies to root overlays and persistent tmpfs mounts with either
disk or memory backing. Buffers belong to each export, so concurrent sandboxes
do not contend over a shared buffer. A final flush is required for success;
write and flush errors propagate to the capture caller. The tar format,
timestamps, permissions, ownership, hard links, xattrs and whiteouts retain
their existing behavior. Metadata export for memory checkpoints uses the same
buffered writer.

## Measurements

These are single matched workloads on Babel's local NVMe, with four guest
vCPUs and an eight-CPU host affinity. Candidate measurements also overlapped
builds and acceptance tests on separate CPU sets. They are not promises for
other storage or host loads. The client's original 526–560-second exports
were **not reproduced** here.

| Payload | Previous export | Updated export | Previous pause interval | Updated pause interval |
| --- | ---: | ---: | ---: | ---: |
| 300,000 files of 8 KiB; 2.72 GiB archive | 8.93 s | 4.67 s | 9.70 s | 5.46 s |
| 3 GiB file plus 20,000 files of 8 KiB | 3.12 s | 3.00 s | 3.90 s | 3.80 s |

The small-file workload improves about 1.9×; large sequential data was already
fast here. These pause intervals include runtime command startup and mount
inventory. Publication and background checksum verification happen afterward.
Sandboxes still pause across the persistent mounts to preserve a consistent
filesystem cut; this is not a copy-on-write snapshot implementation.

Two simultaneous captures, each containing 512 MiB plus 8,192 small files in
both root and Docker storage, each completed their capture interval in 2.40 s.
An unrelated sandbox completed 49 commands during capture: median 20.9 ms,
maximum 51.2 ms. Both saved copies restored and all payload files matched.

The raw measurement summaries are in
[filesystem-export-acceptance.json](filesystem-export-acceptance.json).
`scripts/profile-filesystem-snapshot.py` reproduces the payloads, records
capture/publication/restore timings, verifies content and metadata, and can
collect the runtime CPU profile. Raw profiles and test logs remain in private
`/scratch/pranjala/sandweave-fs-export-20260926` artifacts.

## Progress limit

The worker no longer terminates a filesystem export simply because 600 seconds
have elapsed. It checks output-file growth and allows 600 seconds without
progress. `SANDWEAVE_SNAPSHOT_STALL_TIMEOUT` sets that interval to any positive
finite number of seconds in the worker environment. Invalid values fail before
the sandbox is paused. This is independent of SDK/controller transport timeouts
and restore startup deadlines.

Unit tests cover continued progress beyond the interval, stalled-child cleanup,
nonzero exit status, invalid settings and restoration of the original pause
state after an error. The live small-file candidate used a two-second stall
interval and completed its 4.67-second export successfully.

## Qualification

- The engine's tmpfs test target covers existing tar round trips plus batching,
  multi-buffer files, zero-filled content, partial final buffers and write errors.
- Twenty-one live SDK tests cover large parallel captures, filesystem and memory
  restore, independent clones, self binds, Docker/containerd mounts and cleanup.
- Both previous-runtime and new-runtime archives restored with the new engine;
  every small file in both 300,000-file archives and both 20,000-file archives
  was checked, as well as large-file checksums, hard links, permissions and xattrs.
- Upgrade tests require the new runtime capability for new launches. Older memory
  snapshots remain pinned to their original engine; cold restores use the current
  engine without changing the saved archive format.

An initial new unit-test fixture omitted its root directory and was corrected;
the final test target passed. An old local Bazel installation could not start,
so the engine tests ran from a fresh owned extraction and build directory.

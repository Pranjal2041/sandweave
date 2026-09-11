# Scratch-backed memory measurements

Measured on 2026-09-11 on `babel-p9-16`. A 20 GiB file mapping ran successfully
inside a separate 4 GiB memory cgroup with swap disabled. Performance depended
strongly on access locality: 99% of accesses to a warmed 2 GiB region cost about
5.2 times the RAM baseline; random access across the whole mapping cost about
260 times the baseline. These are native Linux paging measurements made before
SDK integration. The subsequent [implementation and acceptance](disk-memory-support.md)
expose disk backing with an independent storage path and a kernel RAM cap.

## Measured results

All main comparisons used the same initialized 20 GiB data and the same access
sequences. The table reports the median of three trials. Each random trial
performed 200,000 dependent accesses, with different seeds between trials so a
small repeated sequence could not accidentally become a RAM-cache benchmark.

| Work | Anonymous RAM, 24 GiB cgroup | Scratch mapping, 4 GiB cgroup | Slowdown |
| --- | ---: | ---: | ---: |
| Scan all 20 GiB | 0.475 s; 42.10 GiB/s | 16.575 s; 1.21 GiB/s | 34.9x |
| Random reads across 20 GiB | 0.249 microseconds/access | 64.808 microseconds/access | 259.8x |
| 99% of reads within warmed 2 GiB; 1% elsewhere | 0.190 microseconds/access | 0.992 microseconds/access | 5.2x |
| Random read/modify/write across 20 GiB | 0.243 microseconds/access | 71.849 microseconds/access | 295.4x |

The random-write timing excludes the final explicit flush. Including the flush,
the disk-backed median was 72.189 microseconds/access. These operations modify
one word on each selected page, rather than streaming entire pages. The clock
calls, address dependency, and page-header validation are included in access
timings; these are not bare DRAM hardware-latency measurements.

The random-read disk trials incurred 160,809–162,367 major faults per 200,000
accesses and 628–634 MiB of physical reads. The 99%-hot trials incurred only
1,774–1,796 major faults and about 7 MiB of physical reads. A rare disk access
still stalls the program; the low average comes from avoiding most disk reads.
This synthetic locality test does not establish an agent workload's slowdown.

A separate control used the same disk-backed mapping with a 24 GiB cgroup and
the complete file resident in RAM. Its sequential reads reached 43.08 GiB/s,
and random reads took 0.256 microseconds/access, with no physical reads during
those measurements. Merely using a disk-backed mapping therefore does not
make every read slow. Its first write to a clean page still paid Linux's dirty
page tracking cost: random writes took 2.306 microseconds/access before flush.

## Sequential prefetch experiment

A second run kept the 4 GiB cap and tested 32 MiB lookahead in 4 MiB chunks.
Lookahead alone was slower: 17.56 s per 20 GiB scan versus 15.07 s for the
ordinary scan in that run. Lookahead plus releasing each consumed mapping and
its file-cache pages improved the two fully cold passes to 12.210–12.215 s,
about 1.64 GiB/s. Both passes physically read the full 20 GiB. An earlier pass
took 11.089 s but reused roughly 4 GiB already cached; it is not a cold result.

Explicitly releasing consumed data requires knowledge that those pages will
not be revisited soon. It is a streaming optimization, not something arbitrary
guest programs can be assumed to do. Even this variant was about 25.7 times
slower than the RAM scan. These experiments do not establish the device's peak
bandwidth or rule out other pager implementations.

## Memory isolation and storage

The worker ran under an existing Slurm allocation. Each benchmark used its own
step, two eligible CPUs (5–6), and a verified `memory.max`: 24 GiB for RAM and
resident-file controls, 4 GiB for overflow. Its enclosing limits disabled swap.
The overflow cases both peaked at exactly 4,294,967,296 charged bytes. All cases
completed with zero OOM events and zero swap usage. The limit includes file
cache, the benchmark process, and the telemetry process; resident file contents
were therefore slightly below 4 GiB. The node was shared, not reserved solely
for this experiment.

Hardware was AMD EPYC 9354 and a Samsung `MZQL27T6HBLA-00A07` NVMe device.
`/scratch` was local XFS on `/dev/nvme1n1p2`; kernel version was
`5.14.0-687.25.1.el9_8.x86_64`. Both tests ran as the normal user, without
host sudo, KVM, swap activation, or changes to existing cgroups and sandboxes.

The backing file stored all 20 GiB, including pages also cached in RAM. This
prototype therefore requires space for the full mapping, not just 16 GiB of
overflow. XFS speculative preallocation temporarily allocated about 27.9 GiB
during preparation. Only this experiment's backing files were deleted on
successful completion. Raw telemetry and source copies remain available.

A backing file alone does **not** enforce a 4 GiB resident-memory ceiling.
Slurm supplied that enforcement for the experiment. The current Sandweave
allocator limits populated guest pages, including pages swapped by the host;
it does not separately limit resident pages. Its Go-runtime guard also is not
an aggregate host-RSS ceiling. A portable implementation needs per-sandbox
resident-memory enforcement, using delegated cgroups where available or a
pager that enforces the limit itself. A periodic usage check would permit
overshoot and would not establish the same guarantee as this experiment.
CPU and disk I/O contention remain possible even with separate memory caps.

## Reproduction and evidence

The runner creates unique files, writes their full contents, synchronizes them,
and evicts only their own cache before mapping. It checks physical allocation
and uses `mincore` to verify a cold start. It collects major/minor page faults,
physical I/O bytes, per-access latency, checksums, and cgroup telemetry every
100 ms. Huge pages are disabled in both baselines and file mappings, matching
the current guest allocator's behavior when its page budget is enabled.

Run from the intended worker, using an existing allocation on that host. Paths
below must be new; adjust the username and allocation to your installation.
The runner requires GCC, Python, Slurm, and a cgroup-v2 step memory limit.

```bash
python scripts/profile-disk-memory.py --job JOB_ID \
  --scratch /scratch/USER/disk-memory-test \
  --output "$PWD/runs/disk-memory-test"

python scripts/profile-disk-memory.py --job JOB_ID \
  --scratch /scratch/USER/disk-memory-prefetch \
  --output "$PWD/runs/disk-memory-prefetch" --cases overflow --stream-only

python scripts/summarize-disk-memory.py \
  runs/disk-memory-test runs/disk-memory-prefetch \
  --output runs/disk-memory-summary.json
```

The main experiment's exact source is committed as `c69bbc7`; its source hashes
match its manifest. The subsequent prefetch experiment adds the `--stream-only`
path. The retained [measurement summary](disk-memory-profile-20260911.json)
contains every trial, source hashes, evidence hashes, resource limits and
completion receipts for both experiments. Raw evidence is in
`runs/disk-memory-profile-20260911/` and `runs/disk-memory-prefetch-20260911/`.
Source copies and binaries remain under the corresponding private
`/scratch/pranjala/sandweave-disk-memory-*` directories.

The initial 2 GiB smoke experiment is retained separately under
`runs/disk-memory-smoke-20260911/`. Its repeated random sequence could warm the
later write test, and its hot-region warmup was insufficient. Those two methods
were corrected before the main measurements; the smoke timings are not used
in the reported comparisons.

All four accepted cases passed the receipt validator: expected repetitions,
matching data checksums, cold starts, zero OOM/swap, observed physical reads
for disk tests, fully resident RAM controls, and recorded memory peaks within
their caps. No SDK behavior or public API was changed by this profiling work.

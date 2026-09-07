# Asynchronous snapshot verification

Implemented in the standalone lab on 2026-09-07. The gVisor engine and runtime binaries are unchanged at source commit `8c8b1437b27ce0fb61a9db17c5feb33242b7bde3`. No main-project changes, incremental storage, dirty tracking, host privilege changes or KVM are involved.

## Behavior

`checkpoint-gvisor.py ENV LABEL` captures to node-local storage, resumes the guest, and publishes the complete snapshot to persistent storage. It returns without hashing payloads. A detached worker with increased nice value verifies the captured files against the published copy, plus the base image and recorded runtime hashes. Hashes of the same file identity are reused within that verification. The immutable artifact assumption is unchanged; this is integrity checking, not authentication against a hostile host.

New format-2 manifests initially contain file sizes, a snapshot identity and the frozen local source's file identities. `verification.json` is updated atomically through pending/running/passed/failed. Only after all comparisons succeed are the checksum fields published atomically into the manifest. The source signatures detect modifications before/during initial verification. A worker interrupted by allocation termination may leave pending/running status; its hostname and, once started, PID are recorded. Explicit verification can restart it. Keep the frozen local capture until initial verification passes; a checkpoint moved elsewhere before that step cannot prove its first copy against a missing source.

Normal restore checks file sizes, manifest format, base size, runtime identity and file existence without reading all contents. It accepts pending/running verification and rejects a recorded failure. Retrying verification does not clear a known failure until the retry passes. No running guest is retroactively stopped if its snapshot fails verification later. Existing format-1 snapshots remain supported. Fast restore trusts the stored artifacts; it does not detect same-size corruption introduced after a previous check.

For a new snapshot on its original node, restore uses the existing frozen capture if every payload's device/inode/size/mtime/ctime still matches the recorded values. It falls back to the persistent copy when the local capture is absent, changed or belongs to another node. This reuses existing local files; it adds no incremental snapshot format or filesystem dependency.

Explicit checks:

```bash
python scripts/verify-snapshot.py snapshots/LABEL
python scripts/run-gvisor.py --detach --verify --restore snapshots/LABEL NEW_ENV
```

Verification is automatic after save and explicit on demand. There is no new periodic background service. Regular restores do not wait for verification. Save timings are in `snapshots/LABEL/save-timings.json`; launcher setup timings and chosen storage are in `runs/gvisor/ENV/restore-timings.json`. The historical `pause_seconds` field measures the checkpoint CLI duration, not an independently measured pause.

## Measurements

These are short live runs on the shared Slurm node, not controlled cold-cache benchmarks. Removing blocking checksum reads also removes their cache warming. Load and competing activity varied between runs.

Historical save of `full-desktop-ready`: approximately 31.6 s from command/log file timestamps: 11.4 s validation/setup, 6.12 s checkpoint CLI, 3.1 s local hashing, 5.3 s persistent copy, 5.7 s destination hashing/publication. Historical restore of `ready-clone2`: approximately 22.47 s, including 19.68 s validation/setup. The old scripts hashed the same 7.82 GB base twice on save and three times on restore.

First revised round trip (`async-snap1`): 15.44 s inside save / 15.62 s external command wall time; 5.83 s capture and 9.33 s publication. Verification was pending when save returned and running when the clone became ready. Restoring the existing format-1 desktop took 4.38 s to its live state probe. Restoring the newly copied snapshot directly from NFS took 17.78 s: kernel load was 2.34 s, but the 4.86 GB page payload took 16.01 s under concurrent verification. This result prompted reuse of the already-existing local capture.

Second revised round trip (`async-snap2`): external save returned in 15.13 s. Restore setup took 0.434 s. Local page loading took 1.225 s, overlapping kernel load of 2.975 s. The full state probe responded in 7.044 s from the launch command. Verification was still running then and completed independently in 35.74 s. A separate explicit recheck later passed in 9.50 s. Background work still consumes CPU/storage bandwidth; it is outside the blocking workflow, not eliminated.

With `async-snap2`'s local capture temporarily unavailable, persistent fallback restored the same state successfully in 9.56 s to the probe, including 0.99 s setup. The capture was restored to its original location afterwards. These storage measurements demonstrate working paths, not an isolated comparison of NFS versus NVMe throughput.

## Validation and evidence

- `scripts/test-snapshot-store.py`: 14 checks covering nonblocking inspection, a detached worker held behind a verification lock, corruption before/after verification, source mutation before/during hashing, truncation, runtime mismatch, failed-verification retry, format-1 compatibility, path containment and local-source fallback.
- Twelve existing network-policy, Ethernet-relay and CPU-broker checks passed; Python compilation passed. The engine is unchanged, so Go suites were not rerun for these wrapper changes.
- Two desktop round trips preserved RAM nonce/PID, open deleted file and offset, ownership/mode, queued abstract Unix data, established cross-namespace TCP and independence from a post-save source mutation. Moodle returned HTTP200, nested MariaDB was running, and Firefox remained running.
- Actual VNC input opened Moodle's participants page after the second restore. Images: `runs/gvisor/async-snap2-dst/snapshot-async-before.png` and `snapshot-async-participants.png`.
- Structured results: `runs/gvisor/async-snap1-dst/snapshot-performance.json`, `runs/gvisor/async-snap2-dst/snapshot-performance.json`, `runs/gvisor/async-fallback1/persistent-fallback.json`.
- Logs: `runs/gvisor-async-snapshot-performance.log`, `runs/gvisor-async-snapshot-performance2.log`, `runs/gvisor-async-explicit-verification.log`.

The original user desktops and `ready-clone2` are preserved. The implementation recovery bundle is `checkpoints/async-snapshots-8c8b143/`; running snapshots remain separate under `snapshots/`.

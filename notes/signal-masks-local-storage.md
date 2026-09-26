# Signal masks and bounded checkpoint staging

SDK 0.2.33 selects runtime 2026.09.26.3 for new sandboxes. Engine commit
`bbe59b121` backports upstream `0ac45dc2546603605487b21dce5360021789e920`
(Show signal masks in /proc/[pid]/status) onto the previously qualified engine.
The only conflict resolution was adapting test includes/dependencies to this
base. Runtime signal-mask behavior is the upstream implementation, including
per-thread pending/blocked signals and thread-group pending/ignored/caught masks.
No Elasticsearch-specific runtime branches or JDK changes are needed.

The engine is built from a clean upstream archive plus the committed cumulative
patch. Its descriptor and hash-covered capabilities declare `proc_signal_masks`.
Prepared installations upgrade the engine while retaining images. Memory
snapshots keep their captured engine; new launches and cold filesystem restores
use the new engine. Linux 5.4 remains the minimum host kernel.

Worker-local directories use `SANDWEAVE_LOCAL_DIR`, then `TMPDIR`, then Python's
normal system temporary directory. The resolved parent is part of worker
identity, so changing it cannot silently reuse an old worker in `/tmp` or move
a running sandbox. Each workspace has a private random child directory. Explicit
invalid paths raise an error; they do not silently fall back to a RAM filesystem.
Set the variables on the execution worker, including for SSH and Weave workers.

New captures mark their local checkpoint directory as disposable staging.
Restores read the published snapshot even while verification is pending; they
never acquire a dependency on disposable staging. This avoids adding restore
locks, lease scans or another checksum pass. After successful verification has
published content hashes, the verifier removes the staging directory only if
it belongs to this worker and has the matching snapshot identity. Failed
verification retains the source for recovery. Cleanup errors are recorded and
retried on subsequent verification, without invalidating the durable snapshot.
Diagnostic local-only captures and legacy local copies are retained. Existing
legacy readers therefore cannot have their input deleted by the new cleanup.

## Acceptance

On Babel, using disposable sandboxes without host sudo, KVM or GPUs:

- Live Python signal checks covered all five new fields, thread/process pending
  distinctions, handlers, ignored signals, and independent thread blocking.
- The backported upstream `ProcPidStatusTest.SignalMasks` C++ test was compiled
  from the clean source and passed inside a Sandweave sandbox.
- Elasticsearch 9.2.0's unmodified bundled JDK 25+36 successfully attached using
  `jcmd VM.version`. Elasticsearch itself booted with ordinary single-node,
  loopback-only test configuration and returned green cluster health. No attach
  workaround or disabled entitlement checks were used. These two tests passed
  in 61.82 seconds.
- Repeated memory and filesystem snapshots, concurrent independent restores,
  second-generation captures, Docker images/containers/volumes, self binds and
  files larger than guest RAM passed: 14 tests in 163.77 seconds. Disposable
  checkpoint directories were empty after verification while clones still ran.
  During concurrent captures, 23 unrelated commands had a 48.73 ms maximum.
- Focused storage/upgrade tests passed (78); the existing snapshot verification
  suite passed (20), including corruption and asynchronous verification cases.
  The source SDK suite passed with 715 tests and 8 optional skips on Python 3.13.

Reusable tests live in `tests/test_local_storage.py`,
`tests/test_checkpoint_cleanup.py`, and their integration counterparts.
Set `SANDWEAVE_ELASTICSEARCH_ARCHIVE` to the official checksum-verified
`elasticsearch-9.2.0-linux-x86_64.tar.gz` to include the application acceptance.
Local logs are under `/scratch/pranjala/sandweave-signals-20260926`.
Installed-package validation and published artifact hashes are recorded in the
release receipt from `scripts/deploy.py`.

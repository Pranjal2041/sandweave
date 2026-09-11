# Weave concurrency investigation

The supplied 0.2.4 reports distinguish reproducible SDK faults from integration
constraints. The installed source hashes matched across the controller and four
surviving hosts. The 16- and 64-environment stress requests used 256 MiB guest
plus 256 MiB runtime memory. The separate 8 GiB configuration does not explain
those failures.

## Findings and changes

1. **Concurrent publication.** Import calls locked each begin/write/finish
   operation, but did not account for another importer finishing between calls.
   Its publication caused a late identical write to fail as immutable. Completed
   imports now acknowledge matching bytes without writing; differing bytes still
   fail. Repeated finish returns the committed result.

2. **Replica manifest identity.** The manifest comparison included the source
   workspace and snapshot directory. Replication rewrites those two fields.
   Read-only comparison of the client's retained BusyBox import with its
   published revision found exactly those metadata differences and no changed
   file entries. Comparison now excludes those two locations, retaining the
   snapshot digest, recipe, checksums, modes, symlinks and extended attributes.
   Manifest publication uses an atomic replacement.

3. **Pool failure notification.** Reconciliation skipped failed pools while
   existing checkouts continued polling their own pending state. Failure now
   settles pending and claiming leases with the preparation error and cancels
   unused members. Issued leases remain usable. Cleanup does not overwrite that
   error or wait for dead workers before propagating it from a failed context.
   Successful preparation remains recorded after a used member is terminated,
   so older failures do not become consecutive failures again.

4. **Assignment conflicts.** A queued launch could read an allocation after a
   claim advanced its generation, issuing create under the claim's generation.
   A queued claim could similarly take a termination generation. Create, claim,
   observation and termination now share one work key per sandbox; operations
   check their state and generation before acting and applying replies. Claims
   retain their assigned generation. The three client failures each had a
   second creation-acknowledgment event before the claim error, consistent with
   the stale-launch reproduction. The saved journal had already advanced to
   termination, so it does not independently preserve both conflicting intents.
   Another reproduced defect was hashing equivalent dictionaries differently
   depending on insertion order. New assignment and request hashes are canonical;
   older journals retain their existing exact-replay check.

5. **Queue starvation.** The 64-request regression exposed routine observation
   calls filling the bounded executor queue before later launches could run.
   Launches and termination are now submitted ahead of routine polling.

6. **Unreachable workers.** Keeping uncertain reservations is intentional.
   Six unknown records in the client backup do not prove six live guests.
   An explicit `remove_worker(..., lost=True)` or CLI `--lost` permits the
   operator to confirm that an allocation has ended, release its records and
   block that workspace from rejoining. Connection timeouts do not invoke it.
   Late replies cannot undo the release. The client fleet was not modified.

## Client responsibilities and limits

- Guest and runtime memory both count toward admission. The client's audit
  withdraws the earlier effective-capacity headline and identifies a launcher
  budget larger than its step's cgroup. The SDK now also caps advertisements at
  visible cgroup hard limits. This does not distribute a shared parent budget
  among independent workers or unrelated processes.
- The client benchmark let a secondary barrier error hide the original error,
  overlapped runs, and selected BusyBox for a later bash command. These do not
  explain away the SDK import faults; BusyBox failed before that command ran.
- Retaining snapshots and images after pool closure is intentional reuse.
  Automatic bounded retention is not implemented. No automatic deletion policy
  was added, and no client image or saved snapshot was deleted.
- The client's label mismatch was correctly rejected. The audit's separate
  relay stall remains unattributed; a restart alone does not establish a cause.

## Validation

The six core reproductions all fail against an archived copy of commit
`30ea637` (0.2.4), without changes to that source. They pass with this fix.
The full unit suite passes **269 tests**, with four skipped. Final live
acceptance passes **nine tests** in 103.60 seconds, including fixture cleanup.
Its disposable data lives under `/tmp/sandweave-weave-final-20260911` on the test
machine; no client controller, worker or allocation was restarted.

Regression coverage includes publication followed by delayed identical writes,
replica relocation, rejection of different content, 16 processes sharing one
import store, failed-pool waiters, stale create/claim tasks, canonical replay,
confirmed-loss cleanup and rejection of rejoining, and 64 concurrent claims with
every first claim response deliberately lost after commit. Memory tests cover
v1/v2 limits, ancestors, namespaces and explicit budget overrides.

Live acceptance uses disposable workers and controller state. Existing cluster
tests exercise routing, pool isolation, image transfer, jobs, controller restart,
stderr, deadlines, CLI and snapshot restoration. A separate test forces 16
importers to negotiate before publication, delivers delayed writes after
publication, verifies a replica manifest, restores the imported snapshot, and
runs 16 simultaneous leases with independent file writes and commands.

Private controller databases, raw logs and credentials are not included here.

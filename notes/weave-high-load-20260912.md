# High-load worker loss and image preparation

Sandweave 0.2.15 fixes three reported causes of stalled setup. Upgrade the
controller and workers; no public sandbox or pool arguments change.

## Worker loss

Previously, `remove_worker(..., lost=True)` released controller reservations but
left requests awaiting replies and retained dead endpoints as artifact sources.
The controller now cancels those requests and skips the worker's endpoints in
image discovery, transfer and cleanup. Workspace identities cover historical
listener ports; saved worker records restore the exclusion after restart.
Other workers on the same host remain eligible. Ordinary connection failures
still preserve reservations until there is evidence that execution stopped.

Relay cancellation uses the broker's existing request table. HTTP requests use
async sockets with tracked tasks, including requests initiated by synchronous
controller operations. Forwarded requests remain on the serving event loop;
they do not acquire a lifecycle thread or wait for a database commit.

## Image preparation

Previously, same-image transfers acquired a filesystem lock inside each launch
thread. A single slow publisher could occupy the entire launch executor with
waiters. Preparation now has one future per snapshot and actual destination
cache identity. Launches record their dependency and return their threads.
Only the publisher performs the transfer; successful completion resumes the
waiting launches. Each destination worker attaches its own revision record.

The cache identity probe does not take the publication lock. File locks still
coordinate publication across independent processes/controllers; identical
delayed writes remain idempotent. Failed preparation propagates to waiting
launches, and cancellation generations prevent a late result from reviving a
cancelled allocation.

## Checksum reads

Publication previously hashed the same base through snapshot verification,
manifest creation, linking, import completion and snapshot verification again.
Automatic verification now carries private checksum receipts tied to the file's
device, inode, size, mtime and ctime. Linking transfers the receipt to the new
path; copied bytes are verified before publication. Changed file identities
invalidate reuse. Explicit snapshot verification still reads the payload.

Initial base-image verification also accepts ctime-only changes caused by
hard links, before a checksum is pinned. Device, inode, size and mtime must remain
unchanged. Unhashed frozen checkpoint sources retain strict signature checks.

## Acceptance

`tests/test_weave_high_load.py` uses independent in-memory executors with real
controller state and real HTTP/relay request cancellation. In the recorded run:

- 128 blocked HTTP requests were cancelled in 12 ms; 128 relay requests in 3 ms.
- 128 launches waited on one publisher, using one image-preparation thread.
  An unrelated simulated launch took 52 ms and termination took 16 ms while
  publication remained deliberately blocked.
- Lost-source exclusion and pool cleanup passed after controller restart.
- `tests/test_artifact_verification.py` counted five base-image checksum passes
  during publication on installed 0.2.14, versus one on the updated code. A new
  worker attached to the completed shared copy without another checksum pass.
  These count actual checksum reads of a 1 MiB fixture; they are not a GCS
  bandwidth benchmark. Same-size corruption remained rejected.
- `scripts/test-snapshot-store.py` covers initial hard links before/during hashing,
  changed content, strict checkpoint signatures, receipt reuse, and explicit
  re-verification.

The 30-second serving comparison used `scripts/profile-weave-control.py` with
1,000 callers, 500 active records, 10,000 retained records and a simulated 1 ms
worker. Actual HTTP, outbound relay, owner renewal, reconciliation and dashboard
collection remained enabled. Responses were checked against their requests.

| Controller | Requests/s | p50 | p95 | p99 |
| --- | ---: | ---: | ---: | ---: |
| Installed 0.2.14 | 1,936 | 500 ms | 736 ms | 1,515 ms |
| 0.2.15 changes | 1,928 | 498 ms | 810 ms | 1,369 ms |

Throughput differed by 0.4%; these are measurements from one host, not a claim
of increased ordinary request throughput. Raw aggregate results are in
`notes/weave-high-load-20260912.json`. A first implementation duplicated relay
tracking and reduced throughput; the published implementation reuses the
broker's existing request table instead.

Live acceptance uses disposable gVisor workers and outbound bridges. It covers
concurrent imports, sixteen simultaneous leases, shared-cache pool creation,
cleanup of generated files, and preservation of an unrelated sandbox/cache.
No user's running cluster is restarted by this validation.

The final live run passed four tests. Across 16 real sandboxes and 128 commands,
file reads had a 5 ms median and 19 ms p95; complete `env.run` calls had a 98 ms
median and 241 ms p95. A simultaneous burst of 512 completed-status RPCs had a
1.09 s p95. The test also retrieved and checked a complete 3 MiB output stream.
The snapshot verifier passed 20 tests. Host regression coverage also includes
controller shutdown and invalidating receipts when a source changes during copying.

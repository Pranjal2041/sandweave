# Pool reuse and cleanup

Sandweave 0.2.19 adds the remaining pool fixes from the client review.
Async lifecycle waits and indexed status reads were already released in 0.2.18.

## Changes

- Checkout is inside the lease's cleanup scope. A lost acknowledgement still
  causes release of the same lease ID. Uncertain release replies are retried
  until the pool's wait deadline; an original checkout or episode exception
  remains the reported error. Existing transport timeouts still govern each RPC.
- Pending polls retain their 50 ms interval, with waits clipped to the remaining
  deadline. Lease cancellation wakes the wait immediately. The proposed fixed
  one-second interval was not adopted: it would add up to one second of delay
  between readiness and its next observation by the client.
- Image preparation first asks the destination for its native revision. A hit
  registers its location if needed and continues launch without a transfer
  future, shared-cache identity probe, publication, or attachment. A missing
  revision keeps the existing transfer deduplication by actual destination store
  and snapshot ID. Retiring or released revisions remain rejected.
- Placement includes each affinity pool's released, prepared builder. The
  scheduler already supported this record, but the controller previously passed
  only unreleased allocations. Builder records are read by ID with a small
  projection, without scanning historical sandboxes. Released builders reserve
  neither slots nor memory. Their durable records preserve the preference across
  controller restarts; resource, label and draining checks still apply.

## Validation

The host suite passed **447 tests**, with two optional tests skipped. Added
regressions cover lost checkout replies after commit, lost release replies,
bounded retries, cancellation, deadline clipping, native location registration,
retiring revisions, 128 concurrent native-image launches with shared-cache
operations prohibited, and builder affinity across restart. Existing 128-way
image deduplication and 256-way lifecycle tests also passed.

All **13 live cluster checks passed in 153.22 seconds** using the candidate
controller/client source and stock PyPI 0.2.16 workers. Two disposable CPU-only
workers exercised commands, files, jobs, controller restart, 16 concurrent warm
acquisitions, shared-cache spillover, local-pool restoration, and retention.
The worker-affinity test verified that native reuse never created the selected
shared-cache directory. A second worker missing the revision still caused
publication and restored independent writable state.

Logs, JUnit receipts and latency samples are retained under
`runs/weave-pool-reuse-20260912/`. Documentation built in strict mode; all 22 pages,
90 Python examples and 1,567 internal links passed the documentation checks.

Upgrade clients for cleanup and restart upgraded controllers for image reuse
and placement. Public APIs and worker RPCs are unchanged. The gVisor engine
remains at `dd5239ad8db0753e8af7da0ea915df71c5357f7c`.

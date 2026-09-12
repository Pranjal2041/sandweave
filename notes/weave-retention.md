# Pool file retention

Sandweave 0.2.13 adds `retain_baseline=False` to cluster pools. Existing pools
retain their files by default. The option requires a new gVisor baseline and
does not accept saved inputs, cache aliases for preparation, or `keep_on_error`.

## Ownership and cleanup

The pool ID namespaces newly downloaded images and their worker copies. An
existing prepared image can be linked or copied into that namespace. Its original
cache is retained. With `shared_cache`, pool-owned files live under
`<shared_cache>/pools/<pool_id>`; other contents are not reclamation candidates.

Workers record durable pins for sandboxes using an ephemeral baseline, including
restores outside its originating pool. Termination releases the pin only after
the runtime stops. Pool members also remove their stopped writable bundle and
network directory. External mounts are not traversed or removed.

The controller waits for confirmed allocation release and any in-flight baseline
capture before requesting artifact cleanup. It checks references from other
pools, active allocations and named cluster caches. Workers separately check
shared-store pins, local names and saved snapshots from other sources. These
checks run under a per-pool filesystem lock shared across worker processes.

Before deleting payloads, the worker persists a retirement plan listing snapshot
IDs. It removes pool snapshot copies, frozen local captures, imports and image
directories. Subsequent workers sharing that store and retries after interruption
reuse the plan. Small retirement markers remain to reject late imports or starts;
lifecycle records and diagnostics remain available.

Cleanup failures retain the closed desired state and retry after five seconds.
The pool reports `cleanup_error` while blocked and `artifacts_released=True`
after every participating worker acknowledges cleanup. Missing worker support is
detected before launching a pool sandbox; it does not trigger unsupported cleanup
for files that worker never created. Unreachable workers are still unresolved,
not proof that their files or processes can be discarded.

This is reclamation of an explicitly temporary pool, not general eviction by
age or size. It preserves the 0.2.12 async transport, bounded executors and
durable controller state model.

## Validation

`tests/test_pool_retention.py` covers high-numbered socket descriptors, local
snapshot reuse with a configured shared cache, reference protection, concurrent
pins, partial deletion/retry, late imports, failed runtimes, old-worker
negotiation and duplicate-creation safety. Image tests cover reuse of an existing
prepared image without registry requests, downloading or repacking.

`tests/integration/test_weave_retention_live.py` runs real BusyBox image pools
through outbound bridges on two disposable workers. Repeated pool closure checks
payload removal while another running sandbox and its named snapshot remain
usable. A separate failure case checks cleanup after a setup script fails.
Installed-wheel acceptance uses the same tests. Release receipts and logs are
kept under the ignored `runs/deploy` directory.

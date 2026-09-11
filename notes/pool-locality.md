# Pool storage and placement

Sandweave 0.2.7 adds `shared_cache` to local and cluster pools and `affinity` to
cluster pools. Public examples are in the README and `docs/pools.md`.

## Contract

- `shared_cache` is an absolute worker path. It is never expanded against the
  client's home or working directory. The cache contains immutable artifacts
  under `artifacts-v1`; workers retain separate stores, credentials and live state.
- A cache has a random persisted identity created under a filesystem lock.
  Equal path strings do not imply shared storage. Bind mounts of the same cache
  retain its identity; independent directories get different identities.
- Publication checks snapshot and file hashes. A source can publish locally
  visible files with links or copies. Workers on shared storage then register
  their own revision metadata without transferring the payload through RPC.
- For separate node-local stores, the controller serializes transfers by cache
  identity and immutable snapshot ID, then rechecks completion. This coalesces
  simultaneous imports from different workers in that cluster. Filesystem locks
  and existing idempotent chunk handling preserve correctness across controllers,
  although independent controllers can transfer duplicate bytes during a cold
  import. A completed import is reused even if its original source is offline.
- First attachment verifies the saved revision; subsequent leases use the same
  immutable-revision fast path as ordinary saved snapshots. Worker restart or
  pool closure does not delete the cache. Automatic eviction remains separate.
- Runtime materialization can still copy snapshot files or link/copy dependencies
  into a worker workspace. This is not a promise of zero disk copying. Reusing
  the same baseline across pools requires the same saved `cache`/`snapshot` ID.
  Once materialized, a worker checks file identities before reusing that copy;
  later leases do not rehash an unchanged multi-gigabyte shared image.

`affinity="worker"` prefers the pool's first worker. `affinity="machine"`
prefers workers sharing that first worker's Linux boot ID. PID namespaces do not
split machine identity. Missing boot IDs conservatively leave workers separate;
hostnames are not an identity fallback. This describes one kernel instance,
not a hypervisor's physical server. A reboot has a new identity.

Preferences are reconstructed from durable assignments, including a completed
builder, and updated within each placement batch. Resource admission, labels,
GPU eligibility and draining still apply first. Affinity allows spillover;
`placement` breaks ties within a preferred location. Existing `spread`/`pack`
behavior remains unchanged with `affinity=None`. Updating affinity affects only
future placements; changing a running pool's cache path is rejected.

## Validation

Regression tests cover simultaneous imports into shared and node-local caches,
byte counts, private revision registration, interrupted transfers, immutable
write rejection, mount aliases, path validation, machine identity, placement
spillover, and preservation of normal spreading. Live acceptance exercises
independent workers, restored filesystem isolation, replacement after release,
cache retention, and local pools using explicit endpoints.

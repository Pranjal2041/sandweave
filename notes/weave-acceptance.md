# Weave implementation acceptance

Date: 2026-09-09. This covers the initial coordination stage of the
[design](weave-design.md). The [guide](weave-usage.md) describes the available API.

## Final package checks

The package including remote-owner route acknowledgement passed:

| Check | Result | Record |
| --- | --- | --- |
| Source host suite | 177 passed, 2 skipped | `runs/weave-unit-acceptance.xml` |
| Installed-wheel host suite | 177 passed, 2 skipped; 14.89 s | `runs/weave-owned-route-host.xml` |
| Installed-wheel live suite | 7 passed; 232.80 s | `runs/weave-owned-route-acceptance.xml` |

The live run used two dedicated workers and a controller database on the cluster's
NFS4 mount under `runs/weave-owned-route-acceptance/`. All 60 Python package files
in the wheel matched the corresponding checkout files byte for byte. The wheel
and source archive are in `runs/weave-ownership-package/`.

- Wheel SHA-256: `436f22564cba2b815641890b116282fa67c205e9b0ad62ea404f6ce07e3766a6`.
- Source archive SHA-256: `9e168b23c2a8a8d93f9044d046ff641bb688f0ea2769f4445ff442ab632d0131`.
- The gVisor source checkout remains unchanged at
  `c2f78eb0077f8394917b9bcdece2e521e3150d76`.

## Host checks

```bash
python -m pytest -m 'not integration and not gpu' -q \
  --junitxml=runs/weave-unit-acceptance.xml
```

Result: **177 passed, 2 skipped, 72 deselected**. The deselected tests require
explicit runtime or GPU fixtures. New coordination checks exercise reservation
accounting, GPU UUID selection, weighted placement, exclusive leases, stale
assignments, lost launch replies, controller restart, unreachable workers,
draining, owner expiry, retry cleanup, failed pools, missing Python dependencies, snapshot integrity and
replica fallback, cache publication conflicts, and first-use storage selection.
An attached remote job waits until its creator acknowledges the worker route;
the regression check proves no command starts before that acknowledgement.
Owner heartbeats can then renew the worker independently of the controller.

An HTTP controller test submits a detached job without workers, verifies that
submission returns while queued, and reconnects to cancel it. SQLite checks cover
transaction rollback, persistence, backup and exclusion of a second controller.

## Earlier live check with local controller storage

```bash
SANDWEAVE_WEAVE_INTEGRATION="$PWD/runs/weave-acceptance-20260909-4" \
  python -m pytest tests/integration/test_weave_live.py -x -vv \
  --junitxml=runs/weave-acceptance-20260909-4.xml
```

Result at that revision: **6 passed in 278.74 seconds**. Two dedicated worker
processes used CPU sets 32–33 and 34–35, with two slots and a 4 GiB budget each,
inside the existing allocation on `babel-p9-16`. No allocation was created or
cancelled. Worker storage used separate directories; controller state used the
local filesystem. Only these test workers and their sandboxes were cleaned up.

The checks demonstrated:

- Four concurrent coding sandboxes placed across both workers, with commands,
  file reads/writes, and no `/dev/kvm` in the guests.
- Ready pools, pristine state between tasks, ordered `Pool.map` results, async
  acquisition/commands, and pool cleanup.
- An abruptly killed disposable controller while a job was running. Restarting
  the same controller recovered attempt zero and the original stdout, stderr and
  exit code without rerunning the command.
- Durable program uploads, ordered batch results, an explicitly retryable exit
  code, and an execution timeout with a `CommandTimeout` result.
- CLI command stdout, stderr and exit-code propagation.
- Filesystem snapshot transfer through the manifest/chunk protocol, bypassing
  shared-storage discovery. The other worker verified and restored the imported
  snapshot. Repeated transfer negotiation retained the same manifest.

These are two workers on one physical host. They establish the worker protocol
and storage transfer paths, not multi-host network or machine-loss acceptance.

## Earlier package checks

Both a wheel and source distribution build with `uv build --wheel --sdist`.
The wheel installs in a separate virtual environment and imports outside the
checkout. Package artifacts and logs stay under `runs/weave-package/`.

The installed wheel passed **7 live checks in 343.68 seconds**, recorded in
`runs/weave-wheel-acceptance.xml`. The SDK, controller, workers and CLI imported
from `/tmp/sandweave-weave-wheel-bb34e8/lib/python3.13/site-packages`. Pytest's
`pythonpath` setting was overridden to that location.

This run used `runs/weave-wheel-acceptance/controller` for controller state on
the cluster's NFS4 mount. It repeated the six checks above and also submitted a
job without an existing pool, then verified automatic cleanup of the pool that
the job created. The NFS check applies to this single-controller deployment,
not arbitrary network-filesystem configurations or controller-host failover.

After adding the optional-Python-dependency preparation guard, the rebuilt wheel
passed all **23 coordination checks** and the standalone-job live check again
(**65.52 seconds**, including both worker startups and cleanup). These results
are in `runs/weave-wheel-unit-acceptance.xml` and
`runs/weave-final-wheel-acceptance.xml`; the rebuilt artifacts are in
`runs/weave-final-package/`. The reserved `local` target name is also rejected
before setup, with regression coverage in the storage-selection check.

## Deployment boundaries

The implemented controller has one scheduling authority and one trusted account.
It does not provide automatic controller-host failover, project roles, automatic
machine provisioning, gang admission or preemption. Reservations remain held
when a worker is unreachable; this version has no provider fencing with which
to prove that a lost host's old work has stopped.

GPU model/UUID placement has host-side regression coverage. The cluster tests
run coding workloads; desktop, VR and live GPU recovery across machines need
their own acceptance. Existing local runtime tests remain in the host suite and
the repository's separate runtime acceptance records.

Controller scans and history retention are currently simple. Large-cluster
throughput, queue fairness over long runs, and acquisition latency under
contention are not qualified by these checks. No new latency claim is made.

`cluster.backup(path)` copies the SQLite database. Keep the rest of the controller
directory too: credentials and process-owner leases are separate files. Sandbox
disks, snapshots and external volumes need their own preservation.

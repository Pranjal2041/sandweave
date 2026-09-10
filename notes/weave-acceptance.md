# Weave implementation acceptance

Date: 2026-09-09. This covers the initial coordination stage of the
[design](weave-design.md). The [guide](weave-usage.md) describes the available API.

The [dashboard acceptance record](dashboard-acceptance.md) covers browser
monitoring, resource measurements, packaged assets, and live local/SSH checks.

## Default connection addresses (0.2.3)

Normal cluster startup now accepts HTTP on all IPv4 interfaces with an assigned
port and prints HTTP, SSH and reusable browser links. An explicit listener is
still respected; local URLs are labelled accordingly. HTTPS uses the supplied
certificates. Existing running controllers keep their listeners until restarted.

| Check | Result | Record |
| --- | --- | --- |
| Transport checks, including default startup, network-address authentication, distinct ports, restart, explicit loopback and certificate-only HTTPS startup | 14 passed | `runs/cluster-addresses-transport-final.xml` |
| Dashboard API checks and initial transport checks | 26 passed | `runs/cluster-addresses-host.xml` |
| Printed startup link opened in Chromium | 1 passed | `runs/cluster-addresses-browser.xml` |

The default-start test copied both printed join commands from one fresh project
to another and authenticated against a real controller. It replaced worker
installation with a connection check; it did not launch a sandbox. A separate
connection used the host's non-loopback IPv4 address. Both client projects were
on the same host; this is not a new cross-machine SSH acceptance run.

The browser test ran the CLI in a separate process with no listener or transport
flags, then followed the exact printed dashboard URL in two fresh browser
contexts. Both signed in, refreshed and signed out. The credential disappeared
from the address bar, stayed out of network request URLs and local storage, and
was exchanged for an HttpOnly session cookie. The rendered overview was opened
and inspected in `runs/cluster-addresses-browser/startup-dashboard-0.png`.
These controllers had no workers; the page correctly showed an empty cluster.
All test controllers were stopped. User controllers and sandboxes were untouched.

## Explicit connections and outbound workers

Date: 2026-09-10. Added HTTP/HTTPS controller addresses, SSH controller URLs,
outbound worker agents, controller forwarding and worker CPU/GPU limits.

| Check | Result | Record |
| --- | --- | --- |
| Source host suite | 184 passed, 3 skipped | `runs/weave-transport-host-final.xml` |
| Installed-wheel host suite, with optional dependencies | 187 passed | `runs/weave-transport-wheel-host.xml` |
| Existing live suite through outbound HTTP channels | 7 passed; 305.91 s | `runs/weave-transport-live.xml` |
| Real two-host HTTPS/SSH test, including CLI join | 1 passed; 61.69 s | `runs/weave-network-live-verified.xml` |

The network test started a disposable TLS controller on `babel-p9-28` and an
outbound worker agent on `babel-p9-16`. CLI join capped the worker at CPU IDs
32–33, zero GPUs and a 4 GiB memory budget. The test checked repeated join,
commands, binary-backed file RPCs, the absence of `/dev/kvm`, SSH forwarding to
the controller, and a Python client running on the other host. Direct worker
connections were rejected in the local test client. Drain/remove stopped the
agent, and its idle worker and controller were shut down. No allocation was
created or cancelled, and existing user workloads were not changed.

The HTTP live suite exercised commands/files, pristine warm pools, asynchronous
calls, controller crash/restart during a job, batch jobs, explicit retries,
timeouts, CLI exit codes and snapshot transfer/restore through forwarding.
It preceded the final listener validation and agent identity changes; the
two-host test above used the final source, including those changes.

Socket tests cover token rejection, TLS trust verification, an incomplete TLS
connection that must not stall other clients, stable ports and credentials after
restart, binary result correlation, late/duplicate replies, sandbox-scoped
authorization, and saved aliases with absolute credential paths. GPU tests prove
that the device cap intersects the existing Slurm allocation; this change does
not claim a new live GPU workload qualification.

The final bridge retains at most four local worker connections per polling
thread. A real-socket regression cycles through ten sandboxes with one poller
and checks that old scoped connections close. This prevents connection growth
over repeated episodes. The two-host test was repeated after this change.

The wheel and source archive are in `runs/weave-transport-package/`. All 63 SDK
Python files and the bundled GPU eligibility helper matched the checkout bytes.

- Wheel SHA-256: `22d171761d677da1697fc0a64e2bf5c0a94d9972829ad6929709f5a85b75c566`.
- Source archive SHA-256: `c10b2d5e7a201a9639e84dde29448eb6a1e16ce8ba12ee6ea69f3385a839ef08`.

The successful network run followed two test corrections: reading the hostname
from the worker summary, and selecting the ready worker when a reused test
directory also contained removed worker records. The unsuccessful test receipts
and logs remain under `runs/weave-network-live*`.

The forwarding path carries sandbox RPCs; it does not tunnel arbitrary guest
TCP ports. It adds a controller dependency to remote attached-owner heartbeats:
an outage beyond their grace period can expire the sandbox. The existing direct
path can still renew workers independently. The controller remains a service
for one trusted account, not a multi-tenant API, and this test makes no
large-cluster throughput or latency claim.

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

# Dashboard acceptance

Date: 2026-09-10. The [guide](dashboard.md) describes the commands, measurement
definitions, authentication, and retention limits.

## Results

| Check | Result | Record |
| --- | --- | --- |
| Dashboard API, security, collector, and bounded-load checks | 12 passed; 4.29 s | `runs/dashboard-api-final.xml` |
| Dashboard checks including missing aggregate measurements | 13 passed; 4.96 s | `runs/dashboard-aggregation.xml` |
| Final browser interaction check | 1 passed; 7.40 s | `runs/dashboard-browser-reviewed.xml` |
| Final installed-wheel host and browser suite, excluding setup progress | 192 passed; 33.06 s | `runs/dashboard-accepted-core.xml` |
| Final installed-wheel setup progress checks | 9 passed; 28.52 s | `runs/dashboard-accepted-progress.xml` |
| Real coding sandbox and job, through outbound workers | 1 passed; 110.63 s | `runs/dashboard-live-qualified.xml` |
| CLI and browser through SSH to another controller host | 1 passed; 10.04 s | `runs/dashboard-ssh.xml` |

The API checks cover authentication, one-use links, session expiry and logout,
cross-origin rejection, TLS cookie attributes, rejection of browser sessions at
the administrative RPC endpoint, secret omission, stale measurements, history
retention across restart, PID reuse, unavailable GPU counters, and measurements
from older template workspaces. They also check bounded log tails and concurrent
request limits.

The final package suite ran in two groups, totaling 201 passed tests; both
commands exited with status 0. There were 75 runtime/GPU integration tests
deselected. Per-test logs accompany the XML receipts. Earlier combined runs
received signal exit 143 from the command runner, including one after pytest
had written a 200-pass receipt and another during an existing CLI test. No
specific signal sender was established. Splitting out the two slow terminal
progress tests allowed both groups to finish. The interrupted run's disposable
controller was identified by its exact process arguments and state marker,
then shut down through its authenticated API.

A synthetic inventory of 1,000 sandboxes was read through four concurrent HTTP
clients, making 40 paginated requests. The measured median was 5.03 ms, p95 was
7.31 ms, and maximum was 7.97 ms. This is a local API regression measurement with
seeded data, not a qualification of scheduler throughput or a deployed cluster
of that size.

## Browser and runtime checks

Chromium opened the packaged dashboard and exercised navigation, search,
pagination, resource links, GPU details, chart ranges, task stdout/stderr and
attempt selection, live updates, pause/resume, connection loss and recovery,
events, controller logs, sign-out, and the mobile layout. User-supplied markup
remained text. The test recorded no JavaScript or CSP errors. Rendered screenshots
were opened and inspected; a missing single-point chart marker was corrected and
covered by a browser assertion. GPU browser data was explicitly seeded.

The live test used two isolated workers on `babel-p9-16`, with CPU sets 32–33 and
34–35, two slots and a 4 GiB memory budget each. It ran a coding sandbox without
`/dev/kvm`, touched 48 MiB of guest pages, collected runtime CPU and RSS samples,
and opened its runtime logs in Chromium. One recorded sample contained 15 host
processes, 0.0502 CPU cores, and 360,755,200 bytes of summed RSS. These are sampled
runtime process-tree measurements, not a performance benchmark or guest memory
accounting. The receipt is `runs/dashboard-live-qualified/measurements.json`.

A real job printed to both stdout and stderr and exited with code 7. The browser
showed the failed task, exit code, and stderr. Actual runtime screenshots are in
`runs/dashboard-live-qualified/live-overview.png`, `live-sandbox.png`, and
`live-job.png`.

The remote check started a disposable controller on `babel-p9-28`, invoked
`sandweave dashboard ssh://... --no-open`, and followed the CLI's one-use URL in
Chromium. The command kept its SSH tunnel open, and the controller view showed
the remote host. Its screenshot is under `runs/dashboard-ssh/`. This check used
the two hosts' shared filesystem for the test's Python installation and controller
directory; browser traffic traveled through SSH.

Both disposable controllers reported `stopped` afterward. The live worker
markers were removed, and a remote process check found no remaining test
controller. No user desktop, existing sandbox, or allocation was stopped by the
tests.

## Package

The wheel, source archive, installed package, and checkout match byte for byte across 66 Python
files and three dashboard assets. The dashboard needs no separate frontend
installation. These are local validation builds of version 0.1.2; they have not
been published.

- Wheel: `runs/dashboard-package/sandweave-0.1.2-py3-none-any.whl`.
  SHA-256: `28684c6918b6928ebf962124225f31bd273e3cfdd4cd4ffd2521d47535368a81`.
- Source archive: `runs/dashboard-package/sandweave-0.1.2.tar.gz`.
  SHA-256: `e310fce438399b1029b55c7d96d885529f8e685d3f8f3a6dcc61751c9b884355`.

## Test corrections and scope

Earlier attempts exposed test-fixture issues: a cold image check exceeded the
worker startup timeout; an isolated test environment lacked the package; a
memory assertion accepted a sample from before the guest workload started; and
concurrent pytest cleanup removed a running test controller's temporary state
directory. The fixture now accepts an explicit startup timeout, cleans up a
worker that fails before registration, and keeps daemon state with the selected
integration artifacts. The live assertion requires a sample after the workload
starts and touches every allocated page. Unsuccessful receipts remain under
`runs/dashboard-*`; the live and SSH results above followed these corrections.

A final source review also corrected pool and overview aggregation so missing
CPU/RSS samples remain unavailable, while actual zero readings remain zero.
The regression covers initial CPU samples and stale worker measurements. The
live CPU and SSH checks preceded this aggregation correction; the package checks
include it.

The dashboard provides read-only monitoring for the existing trusted-account
controller. It does not add controller failover, team roles, machine provisioning,
external alert delivery, application tracing, or a full-text log archive. The
checks include live CPU workloads and seeded GPU telemetry; they do not qualify
a new GPU runtime or large-cluster deployment.

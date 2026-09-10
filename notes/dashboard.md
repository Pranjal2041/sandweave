# Monitor a cluster

Start a controller and open the dashboard URL printed in the terminal:

```bash
sandweave cluster start lab
```

The dashboard is included in the Python package. It runs on the controller's
existing listener at `/dashboard/`; it needs no Node.js installation, CDN,
separate frontend server, or external monitoring service.

The startup URL includes the cluster credential and signs in directly. Keep it
private, just like the printed worker join link. It can be reused; the browser
removes the credential from the address bar as it signs in.

For an existing cluster, `sandweave dashboard lab` opens a single-use sign-in link. The link expires after
60 seconds; the browser session lasts eight hours. Add `--no-open` to print the
link without launching a browser. Opening `/dashboard/` directly shows a token
sign-in form. The token is exchanged for an HttpOnly, SameSite browser cookie;
it is not saved in local storage. These [cookie attributes](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie)
restrict script access and cross-site use. Sign out invalidates that browser session.
Controller restart invalidates sessions and unused single-use sign-in links;
startup links remain valid while the cluster credential and address are unchanged.

## Connect from another machine

Use a saved cluster name or an explicit controller address:

```bash
sandweave dashboard https://master.example:8765 --token-file ./controller-token.json
sandweave dashboard ssh://user@master.example/path/to/controller
```

For SSH, run the command on the machine with your browser and leave it running
while viewing the dashboard. It maintains a local SSH tunnel. Your existing SSH
configuration supplies login, jump hosts, and other connection settings.
HTTP and HTTPS targets use their supplied address directly. An HTTPS private CA
must be trusted by both the CLI and your browser; `--ca-file` configures CLI trust.
Use HTTPS or SSH when the network does not provide confidentiality.

A reverse proxy can expose the controller under a path prefix. Preserve the
public Host header and strip the prefix when forwarding. For example, a saved
`https://example.org/lab` target serves its dashboard at `/lab/dashboard/`.
Monitoring does not open inbound connections to workers. Existing outbound
worker channels carry inventory and log requests.

Python can also return a sign-in link:

```python
from sandweave import Cluster

cluster = Cluster.connect("lab")
print(cluster.dashboard())
```

Keep the Python process alive when using its SSH tunnel.

## Available views

| View | Contents |
| --- | --- |
| Overview | Running and queued sandboxes, reachable workers, reservations, CPU and memory charts, task outcomes, startup percentiles, and conditions needing attention. |
| Workers | Health, last contact, labels, CPU allocation, memory budget, slots, eligible GPUs, storage, and host network measurements. |
| GPUs | Device models and UUIDs across workers, utilization, device memory, temperature, power, and history. |
| Sandboxes | Lifecycle state, worker and pool, template, runtime, resource settings, measured CPU/RSS, startup timing, and runtime logs. |
| Pools | Capacity, warm target, ready and active sandboxes, waiting leases, weights, priorities, labels, placement, and activity history. |
| Jobs | Task progress, results, assigned sandboxes, stdout/stderr, and saved attempts. |
| Snapshots | Registered saved revisions and replica counts. |
| Events | Persisted scheduling decisions and lifecycle changes, with cursor pagination. |
| Controller logs | Controller host, uptime, RSS, threads, operations in flight, reconciliation health, and a bounded output tail. |

Select a resource to inspect it and follow links to its worker, pool, job, or
sandbox. Worker and pool details can filter the sandbox list. Lists support
search, state and template filters, sorting, pagination, and export of the
displayed page. Event search matches resource IDs and kinds. Charts provide
15-minute, one-hour, six-hour, and 24-hour ranges, plus accessible sample tables.

The browser refreshes at the controller's sample interval. Pause stops automatic
refresh; Refresh requests one update. Failed requests retain the last displayed
data, show an error, and retry with bounded backoff. Missing measurements display
as unavailable. A stale worker is not reported as consuming zero resources.

Dashboard sessions can read monitoring data only. They cannot create, terminate,
drain, or resize resources, or authorize the administrative `/rpc` endpoint.
The deployment still assumes one trusted account; it does not implement team
roles or separate permissions for individual jobs. Logs can contain whatever
applications print, including sensitive application data.

## What the measurements mean

Reservations and measurements are different:

- **Capacity and reservations** are scheduler accounting. Memory reservations
  include guest and runtime budgets. Slots limit concurrent sandboxes. CPU cores
  are shared, so advertised virtual CPUs are not dedicated-core reservations.
- **Eligible CPU busy** measures activity on the worker's eligible host CPU IDs.
  It includes other processes scheduled on those cores.
- **Sandbox CPU** measures CPU time changes in verified runtime process trees.
  One core means one CPU-second per elapsed second. Processes that start and exit
  between samples can be missed. PID birth times prevent attribution to a reused
  PID. These are sampled host measurements, not guest kernel accounting.
- **Sandbox RSS** sums resident pages in those process trees. Shared pages may
  appear in multiple processes, so RSS is not unique physical memory or the
  guest-page allocator's usage. The overview reports measurement coverage.
- **GPU readings** cover eligible NVIDIA devices: utilization, device memory,
  temperature, and power. They include other processes on the same device.
  Missing driver counters remain unavailable; GPU values are not attributed to
  individual sandboxes.
- **Host memory, network and storage** describe the host or filesystem visible
  to the worker. They include other workloads and are not per-sandbox quotas.
  Loopback traffic is excluded from host network rates.
- **Startup percentiles** use recorded worker `ready_seconds` for sandboxes
  created in the last hour. They exclude first-use installation, staging, and
  time waiting for controller placement.

The Linux collector uses the documented
[/proc counters](https://docs.kernel.org/filesystems/proc.html).
It caches worker measurements for five seconds and tolerates unavailable
permissions or hardware. Collection does not change resource enforcement.
Older running workers without this collector can still report scheduling state;
they need the new worker code to report measurements.

## Retention and overhead

By default, the controller samples every five seconds and retains up to 24 hours
of measurements:

```bash
sandweave cluster start lab --monitor-interval 10 --history-hours 48
```

Settings persist in the controller directory. Changing them requires stopping
and restarting that controller; existing sandboxes retain their lifetime policy.
The supported ranges are 1–300 seconds and 1–168 hours. Sampling faster than five
seconds does not make the worker collector sample faster.

Measurements are stored in `monitor.sqlite`, separately from scheduling state.
A background thread builds the monitoring views. Database reads omit binary
program uploads and command output. Browser requests read those cached views;
only an open log view requests output from a worker.

History has a global ceiling of 200,000 samples. Each recorded cluster, worker,
GPU, pool, or measured sandbox contributes a sample. The oldest samples are removed
when either the age or count limit is reached, so larger clusters retain less
history. SQLite reuses freed pages. Deleting measurements does not delete jobs,
events, snapshots, or sandboxes. These have their existing retention policies.

Chart responses contain at most about 180 points, keeping the last sample in
each time bucket. They are not bucket averages or peak measurements. Gaps remain
gaps. Resource pages return at most 200 records; the UI displays 50 per page.
Log responses return the latest 64 KiB by default, with a 256 KiB API ceiling.
The displayed output can be downloaded. This is a log viewer, not a separate
full-text log indexing or archival service.

## Prometheus

The authenticated `/metrics` endpoint exposes worker availability, measurement
timestamps, capacity, reservations, CPU/RSS, GPU counters, host network and storage,
and sandbox/job/task counts. For example:

```yaml
scrape_configs:
  - job_name: sandweave
    scheme: https
    metrics_path: /metrics
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/sandweave-token
    static_configs:
      - targets: ["master.example:8765"]
```

The [Prometheus HTTP configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#http_config)
defines the authorization file setting. The file here contains the raw controller token. Keep it private:
this is the same administrator credential used by the CLI. Browser session
cookies cannot authenticate a metrics scrape. Sandweave does not install
Prometheus or Grafana; this endpoint lets an existing installation retain longer
history or deliver alerts. The built-in dashboard highlights current conditions
but does not send external notifications or collect application tracing spans.

The dashboard adds no controller failover or multi-tenant authorization. Those
remain separate Weave deployment features.

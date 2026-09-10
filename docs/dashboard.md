# Dashboard

Cluster startup prints a dashboard URL. Open it in your browser to view the
controller's workers, sandboxes, pools, jobs, and resource measurements.

For a running cluster:

```bash
sandweave dashboard lab
```

Or print its connection links again:

```bash
sandweave cluster instructions lab
```

The dashboard is included in Sandweave. It needs no separate frontend server or
monitoring service.

## What you can see

| View | Includes |
| --- | --- |
| Overview | Running and waiting sandboxes, worker capacity, resource charts, task outcomes, and startup times. |
| Workers and GPUs | Available devices, resource budgets, health, host measurements, and GPU counters. |
| Sandboxes and pools | Placement, lifecycle state, reservations, active leases, and runtime logs. |
| Jobs | Attempts, stdout, stderr, and exit status. |
| Snapshots | Saved revisions and recorded replicas. |
| Events and controller logs | Scheduling changes, controller health, and recent logs. |

Select a resource to inspect it. Lists support search, filtering, sorting, and
pagination. Charts offer time ranges and sample tables. Pause stops browser
refresh; it does not pause the cluster.

## Configured resources versus usage

Reservations describe the scheduler's accounting. CPU and memory measurements
describe sampled host processes. They are shown separately. CPU counts are not
dedicated core reservations, and summed process RSS is not unique physical
memory consumption.

Missing measurements are unavailable, not zero. GPU counters describe selected
devices, not per-sandbox GPU usage. The default sample interval is five seconds,
with up to 24 hours of retained monitoring history.

## Access and sign-in

The startup URL contains the cluster credential and can be reused. The browser
removes it from the address bar and exchanges it for a read-only session cookie.
Keep that URL private.

`sandweave dashboard` instead creates a single-use link valid for 60 seconds.
Browser sessions last eight hours. Signing out invalidates the current session;
controller restart invalidates sessions and unused single-use links.

For SSH access, run the printed dashboard command on the machine with your
browser. Leave it running while viewing the dashboard: it maintains the tunnel.

Dashboard sessions cannot create, terminate, drain, or resize resources. The
current controller assumes one trusted account; team roles are not implemented.

## Terminated environments

A sandbox can remain listed after termination because the dashboard includes
lifecycle history. A terminated entry is not a running environment. If the entry
still says running after `env.close()`, see [lifetime and cleanup](lifecycle.md):
closing a Python handle does not terminate its sandbox.

## Prometheus and detailed measurements

The authenticated `/metrics` endpoint is available for an existing Prometheus
installation. It uses the controller credential, not a browser cookie.
See the [monitoring reference](https://github.com/Pranjal2041/sandweave/blob/main/notes/dashboard.md)
for metrics configuration, measurement definitions, retention bounds, and the
scope of log collection.

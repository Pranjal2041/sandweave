# Connect a cluster

Weave connects your workers and assigns sandboxes to available resources.
Clients and workers need a route to the controller; they do not need direct
connections to every other worker.

## 1. Start the controller

On the controller machine:

```bash
sandweave cluster start lab
```

This also registers the current machine as a worker. To run only the controller:

```bash
sandweave cluster start lab --no-worker
```

Startup prints the **HTTP address, SSH address, dashboard URL, and complete join
commands**. HTTP and SSH are available together. New controllers choose an
available port automatically.

## 2. Join another machine

Install Sandweave on the worker, then copy either complete join command from the
controller's output. The command already contains the connection details.

No resource flags are required. Without limits, the worker contributes its
eligible CPUs, GPUs, and available resource budget. To contribute less, append
limits to the copied command:

```text
--cpus 4 --gpus 0 --memory 8GiB
```

`--gpus 0` disables GPUs for that worker. Limits respect its existing allocation
and device visibility. `--slots` separately caps concurrent sandboxes.

First-use preparation runs on the worker. After registration, it appears in the
controller's dashboard.

## 3. Create a sandbox

In the project where you started the controller:

```python
from sandweave import Sandbox

with Sandbox(target="lab") as env:
    print(env.run("python -c 'print(2 + 2)'").stdout)
```

From another machine, paste the **printed HTTP or SSH address** as the target:

```python
from sandweave import Sandbox

cluster_address = input("Paste the cluster address: ").strip()
with Sandbox(target=cluster_address) as env:
    print(env.run("python --version").stdout)
```

Paste the address, not the entire CLI join command. Include the `#token=...`
fragment of an HTTP link. The name `lab` is a project-local saved connection;
it does not locate a controller on another machine by itself.

## Open the dashboard

Open the URL printed at startup. To display all connection instructions again:

```bash
sandweave cluster instructions lab
```

The printed dashboard link signs in directly. Treat it and the HTTP join link
as private credentials. For a browser that can reach the controller only through
SSH, run the printed `sandweave dashboard ssh://...` command on the browser's
machine and keep it running while viewing the dashboard.

## HTTP, HTTPS and SSH

| Connection | What it needs |
| --- | --- |
| HTTP | A route to the controller's printed host and port. Credentials are included in the join link. Traffic is unencrypted. |
| SSH | Your existing SSH login to the controller. The printed address includes its state path. |
| HTTPS | A trusted certificate matching the controller hostname, or an existing TLS reverse proxy. |

To configure HTTPS on a new controller, supply its certificate and key:

```bash
sandweave cluster start lab --tls-cert /path/to/server.crt --tls-key /path/to/server.key
```

Startup then prints HTTPS and SSH addresses. For a private certificate authority,
use `--ca-file` when joining, or `ca_file` with `Cluster.connect(...)`.

Advanced deployments can set `--listen HOST:PORT` or
`--advertise https://cluster.example.org`. Advertising an address records an
existing route; it does not create DNS, a reverse proxy, or a tunnel.
Use `--listen 127.0.0.1:0` only when you intend to restrict direct access to that
machine. Such URLs are labelled local.

## Inspect and drain workers

```python
from sandweave import Cluster

with Cluster.connect("lab") as cluster:
    print(cluster.workers)
    print(cluster.info)
```

To stop assigning new work to one worker:

```python
with Cluster.connect("lab") as cluster:
    worker_id = cluster.workers[0]["id"]
    cluster.drain(worker_id)
```

Active leases finish normally. `cluster.resume(worker_id)` allows new placement.
`cluster.remove_worker(worker_id)` unregisters a drained worker after reservations
are released; it does not cancel the machine's allocation.

If a worker is unreachable, its reservations remain charged. A lost connection
does not establish that its sandboxes stopped. Once you have independently
confirmed that its allocation ended, release those records with:

```python
cluster.remove_worker(worker_id, lost=True)
```

The CLI equivalent is `sandweave cluster remove lab WORKER_ID --lost`. This does
not kill remote processes. The removed worker workspace cannot rejoin that
controller; a replacement needs a new workspace. Pending claims receive the
failure instead of keeping a route to that worker.

In 0.2.15 and newer, marking a worker lost also cancels outstanding requests to
it. Image fetching and pool cleanup skip its endpoints, including after a
controller restart. This applies to an explicit loss decision; a temporary
connection failure still preserves reservations.

## Controller lifetime

In 0.2.18 and newer, waiting for worker lifecycle operations does not occupy a
controller thread. Pool lease and status polling also proceeds independently of
state writes. These changes apply to HTTP, HTTPS and SSH connections, including
workers connected through outbound relay. Upgrade and restart the controller to
apply them; existing pool and sandbox arguments stay the same.

```bash
sandweave cluster stop lab
sandweave cluster start lab --no-worker
```

Stopping the controller retains its state directory. Restarting resumes
coordination; worker sandboxes retain their lifetime rules. Long outages can
expire attached-owner heartbeats, so stopping a controller is not a pause of
every workload. Explicit listener settings and credentials are retained.

The current implementation uses one controller for one trusted account.
Controller failover, automatic machine provisioning, team roles, and fractional
GPU admission are not implemented.

# Networking

Sandboxes have internet access by default. To turn it off:

```python
from sandweave import Sandbox

with Sandbox(network="offline") as env:
    print(env.run("python -c 'print(2 + 2)'").stdout)
```

For a cluster sandbox:

```python
with Sandbox(target="lab", network="offline") as env:
    print(env.run("python --version").stdout)
```

Replace `lab` with the full printed cluster address when connecting from another
machine. Offline networking applies **inside the sandbox**. The Python client
and worker can still communicate with the controller and perform sandbox actions.

## Install dependencies before going offline

Worker preparation may download a runtime or template on first use, even when
the requested sandbox is offline. Install software and save a filesystem cache
before running an offline workload:

```python
from sandweave import Sandbox

with Sandbox(setup="./install-tools.sh") as builder:
    ready = builder.cache("tools-ready")

with Sandbox(cache=ready, network="offline") as env:
    print(env.run("python --version").stdout)
```

Provide your own `install-tools.sh`. The second sandbox starts from the saved
files. A setup script that requires internet cannot download packages after
offline mode is enabled.

## Cluster connections

The cluster's HTTP, HTTPS, or SSH address controls how the **client reaches the
controller**. The sandbox's `network` option controls its **outgoing traffic**.
They are separate settings. See [connect a cluster](clusters.md).

Forwarded sandbox commands and file transfers do not expose arbitrary guest TCP
ports. VNC access uses the worker's separate loopback port and your own network
or tunnel arrangement.

## Runtime support

Offline networking is supported by the default gVisor runtime. The native
Apptainer runtime uses the host network and rejects this setting.

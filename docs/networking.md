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

## Use a proxy

```python
import json
from sandweave import Network, Sandbox

with open("proxies.json") as file:
    proxies = json.load(file)

with Sandbox(network=Network(proxy=proxies)) as env:
    print(env.run("curl -s https://api.ipify.org").stdout)
    print(env.info["network"])
```

`proxies.json` contains a JSON list of URLs, for example:

```json
["http://USERNAME:PASSWORD@HOST:PORT"]
```

Keep this file private. You can also pass one URL directly to `Network(proxy=...)`.
Each sandbox randomly selects one supplied proxy and keeps it for its lifetime.
Selection happens separately for each pool sandbox. Random selection can reuse
an address; pass a specific URL when you need an exact assignment. Ten static
proxy addresses provide at most ten distinct exits.

Setup scripts, SDK commands and their child processes receive `HTTP_PROXY`,
`HTTPS_PROXY`, `ALL_PROXY` and their lowercase forms. Tools such as curl and
Python requests use these settings. Browsers and other applications that ignore
proxy environment variables need their own proxy configuration. This mode does
not transparently redirect arbitrary socket calls.

The network policy outside the guest permits outgoing TCP only to the selected
proxy's resolved addresses and port. Direct connections, direct DNS, UDP and
IPv6 are blocked. Ignoring the proxy settings therefore fails instead of using
the worker's public IP. SDK connections and local guest services remain usable.
`env.info["network"]` reports the selected endpoint without credentials.

HTTP, HTTPS and `socks5h` proxy URLs are accepted; the client application must
support the chosen protocol. HTTPS destinations through an HTTP proxy use
CONNECT, preserving destination TLS. `socks5h` requests remote DNS resolution;
Python requests needs its SOCKS dependency for that protocol. Proxy endpoints
must resolve to public IPv4 addresses. Hostnames are resolved on the worker and
pinned in the guest's hosts file for the sandbox's lifetime.

The configured proxy also applies to setup commands. To install dependencies
using direct internet and run episodes through proxies, build a filesystem
cache first:

```python
with Sandbox(setup="./install-tools.sh") as builder:
    ready = builder.cache("research-tools")

with Sandbox(cache=ready, network=Network(proxy=proxies)) as env:
    print(env.run("curl -s https://api.ipify.org").stdout)
```

Worker-side downloads, such as runtime preparation and image imports, use the
worker's connection. They are separate from traffic inside the sandbox.
Memory snapshots retain the selected proxy and its resolved addresses.
Filesystem-cache restores may select another proxy or switch to internet/offline
mode. Private sandbox records and snapshots can contain the supplied credentials.

The [QUEST-RL measurements](https://github.com/Pranjal2041/sandweave/blob/main/notes/quest-proxy-assessment.md)
compare the same research requests directly and through ten proxies.

## Cluster connections

The cluster's HTTP, HTTPS, or SSH address controls how the **client reaches the
controller**. The sandbox's `network` option controls its **outgoing traffic**.
They are separate settings. See [connect a cluster](clusters.md).

Forwarded sandbox commands and file transfers do not expose arbitrary guest TCP
ports. VNC access uses the worker's separate loopback port and your own network
or tunnel arrangement.

## Runtime support

Offline and proxy networking are supported by the default gVisor runtime. The
native Apptainer runtime uses the host network and rejects these settings.

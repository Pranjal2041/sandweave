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

## Connect sandboxes

Create a service network, then join independent sandboxes to it. This works with
any gVisor template, including custom GNOME desktops:

```python
from sandweave import Sandbox, create_service_network, delete_service_network

target = "lab"  # Or the full cluster address. Omit target for local sandboxes.
group = create_service_network(target=target)
api = desktop = None
try:
    api = Sandbox(target=target, service_network=group,
                  aliases=["api"], networks=["private"], network="offline")
    api.exec("python -m http.server 8000 --bind 0.0.0.0")
    desktop = Sandbox(target=target, template="gnome", service_network=group,
                      aliases=["desktop"], networks=["private"], network="offline")
    # The desktop can reach http://api:8000, including from its browser.
    print(desktop.info["service_network"])
finally:
    for env in (desktop, api):
        if env is not None:
            env.terminate()
            env.close()
    delete_service_network(group, target=target)
```

The server binds inside its sandbox. This does not publish a host port.
`network=` still controls outside access: offline members can talk to peers,
but cannot reach the internet. Internet, allowlist and proxy policies continue
to govern all other traffic.

- `service_network` identifies the group and its worker. All members are placed
  on that worker. If it is full, cluster requests wait for capacity rather than
  moving to another worker; `startup_timeout` bounds that wait.
- `networks` names the segments a member can use, defaulting to `["default"]`.
  Members must share at least one name to communicate, even by IP address.
- `aliases` supplies DNS names, defaulting to `[]`. Names become visible when a
  member joins, including to already-running peers. An alias must be unique
  among members sharing a named network.
- `env.info["service_network"]` contains the ID, aliases, named networks and
  service IPv4 address. That address remains fixed for the member's lifetime.

Private packets pass through the worker's hub over private local sockets.
The hub checks membership and assigns the source identity; guests cannot choose
another member's source address or bypass the hub to reach a different segment.
This is a worker-local network, not an encrypted link between workers. A member
of two named networks can intentionally act as an application gateway between
them, so grant multi-network membership only where intended.

Termination removes membership. `close()` alone disconnects an ordinary sandbox
handle; it does not terminate its runtime. Networks persist until explicitly
deleted, and deletion refuses active members. A lost worker's network does not
migrate; create a new network for replacement sandboxes.

For proxy-aware tools, include peer aliases in their proxy bypass setting
(for example `NO_PROXY=api`), so private requests use the service network. An
unknown DNS name falls back to the sandbox's ordinary egress policy.

Filesystem caches are supported. Restoring one creates fresh membership when
you explicitly pass `service_network=...`; it does not restore a source member's
address. Memory checkpoints of joined members are unsupported because rolling
back one member independently would also roll back live connections to peers.
Existing Harbor service groups remain separate and cannot join another group.
The client, controller and workers need Sandweave 0.2.24 or newer.

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

## Restrict destinations

Allow only selected hostnames or IPv4 addresses:

```python
from sandweave import Network, Sandbox

network = Network("allowlist", allowed_hosts=("pypi.org", "*.pythonhosted.org"))
with Sandbox(network=network) as env:
    print(env.run("python -m pip install requests").returncode)
```

The policy permits IPs returned by DNS for those names. It does not check HTTP
Host or TLS SNI names when sites share an IP. Unlisted destinations are blocked;
IPv4 addresses and CIDRs are also accepted. This requires the `0.2.20rc7` preview
on the client, controller and workers. Harbor tasks apply their phase policies
automatically.

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
By default, each sandbox randomly selects one supplied proxy and keeps it for its
lifetime. Selection happens separately for each pool sandbox. Random selection can reuse
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

## Proxy policies

`ProxyPolicy` controls which proxies are eligible and how a pool distributes
them. The policy is configuration; each pool maintains its own assignment state.

```python
import json
from sandweave import Network, Pool, ProxyPolicy, Sandbox

with open("proxies.json") as file:
    proxies = json.load(file)

network = Network(proxy=proxies, policy=ProxyPolicy("same_region"))

with Pool(size=8, warm=2, network=network) as pool:
    with pool.acquire() as env:
        print(env.info["network"])
```

For region policies, group URLs by the region labels you supply:

```json
{
  "uk": ["http://USER:PASS@UK_HOST_1:PORT", "http://USER:PASS@UK_HOST_2:PORT"],
  "us": ["http://USER:PASS@US_HOST_1:PORT", "http://USER:PASS@US_HOST_2:PORT"]
}
```

Labels are matched exactly. Sandweave uses this supplied metadata; it does not
infer location from the URL. Other policies also accept a flat URL list or one URL.

| `distribution` | Behavior within one pool |
| --- | --- |
| `random` (default) | Each new sandbox independently selects a random eligible proxy. |
| `round_robin` | Successive sandbox assignments cycle through eligible proxies. |
| `same_proxy` | Select one proxy once and use it for every sandbox. |
| `same_region` | Select one supplied region uniformly, then cycle through that region's proxies. |

Restrict any policy to a specific region inside the policy itself:

```python
network = Network(proxy=proxies, policy=ProxyPolicy("random", region="uk"))

with Sandbox(network=network) as env:
    print(env.run("curl -s https://api.ipify.org").stdout)
```

A standalone sandbox selects one eligible proxy and keeps it. With `same_region`,
it first selects a region, then a proxy in that region. Sharing a `Network`
object between independent sandboxes does not create a shared rotation counter.

Pools coordinate assignments across all their workers. Region labels are sorted
before flattening a grouped catalog; URL order within each region is preserved.
Rotation follows sandbox creation order. Warm sandboxes can become ready and be
leased in a different order. Preparing the baseline does not consume a position
in the member rotation, but uses the same region/proxy constraint.

Each sandbox keeps its proxy across requests, pause and resume. Retrying the same
assignment keeps that proxy; a replacement sandbox receives a new assignment.
Cluster pools save their chosen region or proxy and rotation position with their
assignments, preserving them through controller restarts and `Pool.connect()`.
Local pools keep this state in their owning Python process. Each pool has an
independent selection, even when several pools use the same `Network` object.

An assignment never silently switches to another proxy, region or direct internet.
`same_proxy` selects the same endpoint; whether the provider changes that endpoint's
exit IP is controlled by the provider. `env.info["network"]` includes the supplied
region and explicit policy when present, along with the credential-free endpoint.

Pool proxy policies use filesystem baselines, including the baseline a pool
prepares automatically. A memory snapshot already captures a particular proxy;
restore it with `Sandbox(snapshot=...)` to retain that binding. Explicit proxy
policies on a pool reject memory baselines rather than changing captured network
state. Filesystem caches can be reused with another policy or region.

Proxy policies require Sandweave 0.2.10 or newer on the client, controller and
participating workers. Older processes that would ignore a policy are rejected.

## Cluster connections

The cluster's HTTP, HTTPS, or SSH address controls how the **client reaches the
controller**. The sandbox's `network` option controls its **outgoing traffic**.
They are separate settings. See [connect a cluster](clusters.md).

Forwarded sandbox commands and file transfers do not expose arbitrary guest TCP
ports. VNC access uses the worker's separate loopback port and your own network
or tunnel arrangement.

## Runtime support

Offline, proxy and allowlist networking are supported by the default gVisor runtime. The
native Apptainer runtime uses the host network and rejects these settings.

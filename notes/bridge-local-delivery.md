# Bridge packet duplication

SDK 0.2.30 pins runtime 2026.09.26.1. The fix is in the engine, and applies to
all templates using bridges. IP forwarding remains enabled. There are no
application-specific firewall rules or connection retries in the fix.

## Cause

`bridgePort.DeliverNetworkPacket` forwarded Ethernet frames between ports and
then unconditionally delivered the same frame to the bridge's network
dispatcher. With IP forwarding enabled, an ordinary switched unicast also
traversed the IP routing path. The receiver saw both the original SYN (TTL 64)
and a routed copy (TTL 63, with the bridge's source MAC).

Once conntrack is active, its no-op source NAT path can remap source ports to
resolve tuple conflicts even without explicit NAT rules. The duplicate path
therefore could produce SYNs for different connections, followed by resets.
The reproducer captured source ports that the client never used. Removing the
extra IP delivery fixes this without changing conntrack or disabling forwarding.
The separately reported historical `packet already had NAT` panic was not
reproduced here, and this change does not claim to resolve every cause of it.

The bridge now delivers its own unicast and broadcast/multicast to the local
network dispatcher. It forwards other unicast only at the link layer, while
retaining packet-socket visibility. Frames addressed to the bridge itself are
no longer also flooded to its ports. Packet type is classified against the
bridge address, rather than retaining classification against the ingress port.
This matches the distinction between local delivery and forwarding in
[Linux's bridge receive path](https://github.com/torvalds/linux/blob/master/net/bridge/br_input.c).

Snapshot acceptance exposed an additional packet-capture defect: the NIC's
saved endpoint list already contained the socket that `endpoint.Restore`
registered again. Each captured frame was delivered twice after restore.
Registration is now idempotent. This check runs when registering a socket,
not on the packet delivery path, and unregistering removes it completely.

## Reproduction and validation

`scripts/probe-bridge-network.py` creates two namespaces, a veth pair per
namespace, and a bridge inside a disposable sandbox. It uses standard Python
TCP sockets and an AF_PACKET capture. It needs neither Docker containers nor
application code. The same script ran in a native Linux user/network/mount
namespace without host sudo.

- Native Linux: 20,000 connections, no failures and no routed SYN copies.
- Runtime 2026.09.25.1, Docker template with filter/NAT rules flushed and
  forwarding enabled: 251 failures in 20,000 connections with eight callers;
  19,999 routed SYN copies and 215 SYNs outside the client's source-port range.
- Patched engine, identical eight-caller case: 20,000 connections, no failures,
  exactly 20,000 SYNs at TTL 64 and none at TTL 63.
- Separate sequential and 16-caller tests: 40,000 connections, no failures,
  exactly one captured SYN per connection. Commands in an unrelated sandbox
  had medians of 19.9/22.0 ms and maxima of 47.6/50.7 ms during these runs.

These are individual measurements on Babel with eight eligible CPUs, not a
throughput guarantee. Packet-statistics counters are unimplemented in the
current guest and return zero; the probe reports them as unknown, and the
regressions compare the actual captured port set and packet counts instead.

Engine tests cover learned/unknown unicast, local bridge unicast, broadcast,
multicast, packet type, and packet-socket visibility for IPv4, IPv6 and ARP.
The stack/conntrack, IPv4, IPv6 and nftables suites also pass.

Live regression coverage is in `tests/integration/test_bridge_network_live.py`:
same-bridge load, unrelated sandbox responsiveness, real Docker bridges,
published ports, cross-bridge DNAT, outbound HTTPS, IPv6 neighbor discovery/TCP,
and a memory restore of running Docker containers and their capture socket.
The default `./deploy --check` validation runs these regressions as well.

The final clean-built runtime passed all four live cases in 77.19 seconds:
80,000 load connections, zero failures, and exact SYN counts before and after
memory restore. Published ports, cross-bridge DNAT, outbound HTTPS and IPv6
passed. [Recorded acceptance](bridge-network-acceptance.json) includes the
runtime commit, before/after measurements and unrelated-command latencies.

Docker's `network create --ipv6` encountered a pre-existing missing
per-interface IPv6 setup sysctl. That configuration path is not claimed as
supported by this fix. The IPv6 bridge data path is exercised directly with
network namespaces. No host IPv6 connectivity is required for that test.

## Upgrade

Update the SDK and restart workers. First-use preparation upgrades an engine
missing `bridge_local_delivery` while retaining prepared guest images. Create
new sandboxes for the corrected engine. Already running sandboxes and memory
snapshots retain their recorded engine; recreate a memory baseline to adopt the
fix. Filesystem caches can start with the current engine.

The client report mentioned additional packet captures on a separate AWS host.
Those files were not available in this workspace; the diagnosis and measurements
above use an independent local reproduction.

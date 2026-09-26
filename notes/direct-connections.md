# Direct client connections

`Sandbox(..., connection="direct")` keeps Weave placement and lifecycle
coordination, then connects directly to the assigned worker for data traffic.
`"cluster"` is the default, including local named clusters. Worker targets were
already direct and retain that behavior. The same selector applies to reconnects,
pool leases, and benchmark pools, including Harbor native service groups.

The existing allocation route supplies a sandbox-scoped token. Direct clients
cannot use that token to inspect another sandbox or administer a worker. A
same-host client uses loopback; remote clients use the registered SSH address
(or worker hostname) and a persistent tunnel. Worker registration preserves SSH
ports; joining workers advertise their login. No new public listener is created.
An outbound-only worker needs an independently reachable SSH route for direct
mode. There is no silent controller fallback.

Each connection caches its routes and persistent sockets. Initial authentication
uses a bounded describe request before handing out a new sandbox or lease.
Async connection setup runs off the caller's event loop. Established async RPCs
use its event loop directly. A changed route discards the obsolete socket; an
uncertain mutation invalidates the route and propagates its error without replay.
Creation failures request allocation cancellation, and lease attachment failures
stay inside the existing release scope.

Scheduling, worker monitoring, owner renewal, lease release, snapshot registration
and cancellation keep their existing contracts. Direct access is a client
preference, not a portable sandbox resource or a cache-key component. It does
not remove ownership deadlines or provide controller high availability.

## Client fork comparison

Inspected the client's `sandweave-ack-latency` fork and
`weird-cua-bench/docs/fast-http-profile-20260922.md`. Its process-output waiting,
timestamped capture/cursor, and JPEG patches are distinct from routing. Its
39–47 ms HTTP measurements also include benchmark-server work and JPEG encoding.
This change keeps existing desktop screenshot/step semantics and raw RGB payloads;
it does not claim to reproduce that complete benchmark or merge those patches.

## Live acceptance, 2026-09-26

Ran `scripts/profile-direct-connections.py` on babel-u9-24 in the existing
allocation, CPU affinity 32–47, no GPU. A disposable GNOME sandbox used 4 GiB
guest RAM, 1 GiB runtime RAM and 1920×1080 RGB observations. Both paths reached
the same sandbox. The cluster path used an actual outbound worker relay. These
are warm, same-host observations, not a cross-region or fleet capacity claim.

| Operation | Cluster median / p95 | Direct median / p95 |
| --- | --- | --- |
| Input acknowledgement, 50 requests | 3.664 / 4.516 ms | 2.442 / 2.674 ms |
| Input plus RGB image, 50 requests | 35.458 / 40.463 ms | 22.520 / 23.255 ms |
| 16 concurrent callers, 200 actions on one desktop | 33.807 / 56.580 ms | 37.011 / 45.061 ms |

The shared-desktop concurrent median did not improve; direct routing does not
remove input-server serialization or worker contention. All requests completed.
The measurement recorded 310 controller data RPCs through the relay and zero in
direct mode. Eight concurrent real coding guests completed 16 pool episodes;
command median/p95 was 43.295/67.804 ms. Every reservation was released.

Typed `Direct input works: hello` into a GTK entry through the public API, checked
the application's saved text, and opened the resulting screenshot to confirm the
desktop and text. The first probe had a test-script race reading that file before
the GTK event arrived; the probe now waits for it and drains cleanup before
stopping its controller.

Source checks: 740 passed, 11 skipped, with integration/GPU tests excluded;
additional focused direct/transport/VNC checks include setup cancellation
draining. Tests use real RPC
sockets for routing, scoped authorization, concurrent calls, uncertain actions,
failed creation cleanup and direct VNC streams. Pool mode propagation and failed
attachment release are also covered.

An additional live test ran eight concurrent direct leases against two unmodified
installed 0.2.34 workers through a current client/controller. Async execution,
process waits, binary transport, file reads/writes, reconnects and cleanup passed.
No gVisor changes or runtime release are needed for this feature. Cross-host
SSH endpoint/port selection is unit-tested; the latency measurements above use
loopback, not a remote SSH host.

The documentation browser check passed, including copying the direct-connection
example. The new section and the live desktop screenshot were opened and visually
inspected. Local artifacts are retained under `runs/direct-connections-20260926/`.

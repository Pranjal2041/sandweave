# Public service networks

Added in 0.2.24. `create_service_network(target=...)` returns an ID;
`Sandbox(service_network=id, aliases=[...], networks=[...], target=...)`
joins it. `network=` remains the egress policy. This is independent of the
template; it uses the existing native service-group IPv4 router.

## Placement and authority

The controller records one owning worker and pins member allocations to it.
It does not reserve slots for an empty network. Normal admission applies and a
full worker cannot spill members elsewhere. Create/delete RPC waits use the
existing async lifecycle scheduler and shutdown drain.

An immutable template installation can give the same worker allocation a new
SDK workspace. A private registry under `SANDWEAVE_HOME/service-networks` points
to the owning hub's worker metadata. Other workspaces in that allocation use
authenticated control RPCs for membership, while packets go directly to that
hub's private Unix socket. Boot/PID namespace, cgroup, CPU affinity, scheduler
allocation and explicit resource settings must match. Available RAM is not an
identity: it changes as sandboxes run. Neither the controller nor a guest can
use this registry to turn the service network into a cross-worker network.

## Packet path

The hub authenticates the registered host socket, assigns a fixed source
address, and checks for a shared named network. All service-prefix packets go
to the hub, including denied or nonexistent destinations; they never fall
through to ordinary host routing. Guests cannot choose another source identity.

Alias lookup is indexed at the hub. An already-running member can resolve later
joins without rewriting every member's configuration. Unknown names return to
the requesting relay and its normal egress policy. Writes to the passt stream
are framed and serialized between that return path and ordinary guest packets.
No controller RPC, disk access or full membership scan occurs in packet routing.

## Lifetime and saved files

Membership is persisted before guest launch. Termination removes its route and
aliases. Duplicate joins/leaves and deletion retries are idempotent. Empty
networks remain until explicit deletion; deletion refuses active members.
The hub rebuilds routes from saved membership on worker restart. Explicitly lost
workers are never queried during network deletion or used for new placement.
After worker loss, replacement sandboxes need a new network.

Filesystem snapshots retain the recipe but omit the private source route.
Restores register a new member/address. Live member memory checkpoints are
rejected because independently rolling back a network participant also rolls
back connections to its peers. Existing Harbor service groups keep their
existing lifecycle and cannot be nested into a public network.

## Focused validation

The heavy release suites are intentionally omitted for this update, as requested.
`./deploy --tests ...` records the explicit selection and still checks the wheel,
source distribution, reproduction from source and published artifact hashes.

- Unit tests exercise 64 concurrent registrations, alias conflict rollback,
  named-network isolation, fixed source identity, late DNS joins, route recovery,
  address reuse, lost workers and strict worker placement. Related transport,
  lifecycle and locality checks include 256 outstanding lifecycle operations.
- `tests/integration/test_service_networks_live.py` exercises local and HTTP
  controller creation, concurrent sandbox launches, DNS, TCP source identity,
  isolation between segments/groups, termination and alias reuse, ordinary
  egress policy, filesystem restores and a custom GNOME template.
- `scripts/check-docs.py --browser` opens the new networking section and copies
  the complete API/cleanup example. The rendered section is also inspected.

Live tests require explicit disposable storage and runtime inputs; desktop
acceptance uses `SANDWEAVE_NETWORK_DESKTOP` as the screenshot destination.
`SANDWEAVE_NETWORK_DESKTOP_ASSETS` optionally supplies a different prepared
desktop installation to exercise joining across template workspaces. Logs,
screenshots and build artifacts remain outside Git; the release receipt records
the selected test files and immutable package hashes.

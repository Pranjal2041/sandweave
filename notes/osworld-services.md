# OSWorld service support

The `0.2.20rc2` OSWorld template selects optional engine support for the original
Avahi, console palette and sysctl services. The official image, shared base
filesystem, original service units and the reference desktop delta are unchanged.

| Template option | Behavior |
| --- | --- |
| `virtual_consoles = true` | Private console devices support keyboard-type queries and reading/writing the 16-color palette. Guest console permissions apply. |
| `netlink_address_events = true` | Route-netlink sockets can subscribe to IPv4/IPv6 address additions and removals in their network namespace. Interface removal and namespace moves also notify subscribers. |
| `sysctl_reapply = true` | The guest PID allocator uses IDs below 4,194,304. Writes reapplying that bound and the platform's actual minimum mmap address succeed; unsupported values fail. |

These are sandbox options, not host configuration. The PID limit changes the
allocator's actual range; it does not reserve millions of processes or allocate
a PID-sized array. The default coding template retains its previous PID range
and multicast subscription support. Console state and network subscriptions are
included in live snapshots.

## Build

- Engine commit: `942b66253d03cf260d2ae8a1665dfabb8723b2d2`.
- Cumulative patch SHA-256: `5d6ff5abb2a7ec57d8cb9e5aec2f2294c2686b2420f4cc50ad5cff297d2bfa86`.
- Clean runtime build: `4d72f51230861e63b4385c5c1e7a42a06f7003f3887c6861346f29065626fc7c`.
- Runtime release: `2026.09.14.2`.

The release was built in a fresh directory from the pinned upstream archive and
committed cumulative patch. The archive contains the engine, provenance and
license notices; it contains no OSWorld filesystem or private reference code.

## Acceptance

The clean release build passed every service probe on a fresh OSWorld boot and
again after a full desktop memory snapshot restore. All three original units
reported `Result=success` and exit status zero. Both runs checked interface
removal notifications as well as explicit address changes. See the
[recorded results](osworld-services-summary.json).

Run the original services, exercise their underlying behavior, and repeat after
a live snapshot restore:

```bash
python scripts/accept-osworld-services.py \
  --source /path/to/unchanged/cua-speed-run \
  --output /path/to/results --restore-live
```

The probe checks an actual UDP DNS response from Avahi, IPv4/IPv6 address
notifications, network namespace isolation, console palette readback and write
permissions, sysctl readback and rejection of unsupported values, and a real
`mmap` below the advertised floor. It also records each original unit's exit
status, its journal and a desktop screenshot.

The separate integration test checks preserved socket memberships and palette
state across a live snapshot, independent palette writes in the clone, and an
ordinary coding sandbox running alongside it. Existing concurrent guest-service
and benchmark lease tests cover unrelated command progress and cleanup.

Current source checks passed: five engine test targets and 58 focused SDK tests.
The four live integration tests passed against the clean release build in 26.93
seconds. During eight concurrent 256 KiB service exchanges, another sandbox
completed 20 commands in 0.72 seconds. Twelve tasks through four concurrent
benchmark leases completed in 11.89 seconds.
These are individual measurements on the qualification host, not throughput
claims for an OSWorld fleet.

The first extended snapshot probe caught an existing `getsockname()` omission:
netlink group membership was not reported. The corrected implementation returns
the subscription bitmap, and the restored socket check now passes. The failed
attempt and successful rerun are retained under
`runs/osworld-acceptance/20260914-services`.

The Chrome font, VS Code workspace and GNOME timezone examples each scored zero
before their recorded keyboard/mouse actions and 100 afterward with the clean
release engine. All 22 screenshots decoded at 1920×1080. The same task hooks,
verifier and action fixtures were used unchanged.

## Scope

Avahi DNS replies are tested inside the sandbox; this does not expose multicast
discovery on the host LAN or bridge it between sandboxes. The added address
notifications cover explicit address configuration and interface lifecycle,
not automatic IPv6 SLAAC/DAD state transitions. The private headless palette has
no host console or physical display attached. Other sysctl values remain rejected.

RealtimeKit and hardware GPU detection remain outside this change. The
[original acceptance record](osworld-acceptance.md) contains their observed
differences and the pinned image/application versions. Modal-native explicitly
selects a VM runtime in the private reference; it is not equivalent to no-KVM
gVisor merely because both use containers at the API level.

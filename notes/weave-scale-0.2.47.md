# Weave and worker scaling, 0.2.47

A client proposed a patch from runs of 8 worlds of 53 machines (424 GNOME
desktops) on one 192-core worker, `aws-3`, on October 23, 2026. Its comments
record the measurements on that host; this repository did not reproduce them.
0.2.47 includes most of the patch, with changes below. Raw measurements are in
[weave-scale-0.2.47.json](weave-scale-0.2.47.json).

## Included

- Worker admission iterates the worker's active-record index instead of every
  record file. Creating, preparing, ready and paused sandboxes hold their
  reservation without a runtime query; other active records are queried until
  their runtime is seen gone. The gone set lives on the worker and is pruned to
  active records.
- Worker telemetry builds one parent-to-children map per sample.
- The controller's name check reads only unreleased allocations' names. Its
  0.1 s loop copies only the fields it reads and checks each owner once.
- Probes ping workers for liveness. The inventory runs at most every 30 s, with
  a 120 s timeout. A ready worker becomes unreachable after 60 s without a ping
  answer, or, on a relay, without any successful reply.
- The relay queues `ping` and `inventory` ahead of other requests and records
  when each channel last returned a successful reply.
- A launch on the worker instance already accepted and promoted skips
  `inventory` and `_accept_worker`. The prepared token is the worker's stable
  instance token, so the shortcut applies whenever the template is prepared.
  Resource-change checks and telemetry then wait for the next probe inventory.
- The service-network router and peers send through a non-blocking duplicate of
  their bound socket.
- Ended sandboxes in `status()` are limited to the 50 most recent, with
  `sandboxes_omitted`.
- The GNOME readiness default is 600 s instead of 120 s, still capped by the
  sandbox's remaining startup deadline (300 s by default).

## Changed from the proposal

- Observation throttling applies only to ready, leased and paused sandboxes. The
  proposal observed every running allocation at most every 30 s, which delayed
  recovery of an `unknown` launch outcome after a lost reply by up to 30 s;
  `tests/test_proxy_policy.py::test_worker_retry_and_claim_keep_exact_pool_assignment`
  timed out. The per-allocation timestamps are pruned to live allocations.
- The proposal limited starts to 160 per worker (`WEAVE_MAX_STARTING`). The
  default is now one per worker CPU and at least 16; `SANDWEAVE_MAX_STARTING`,
  validated at controller startup, sets one limit for every worker. The
  proposal also counted every `starting` allocation, including ones waiting for
  a shared image. Those return their launch thread, so a pool of 128 waiting on
  one image filled the limit and the other 96 never registered as waiters
  (`tests/test_weave_high_load.py::test_128_image_waiters_leave_launch_and_cleanup_threads_available`).
  Only launches in progress count, and launches resumed after an image wait or a
  controller restart are admitted under the same limit.
- `status()` sorts ended allocations by a projected `updated` field and reads
  only the 50 it returns.

## Not included

The proposal selected gVisor's KVM platform whenever the host's `/dev/kvm` was
readable and writable, and removed the launcher's assertion that `/dev/kvm` is
absent. On Babel nodes `/dev/kvm` is `crw-rw-rw-`; Apptainer's `--contain` hides
it from the container today. The host-side check would have moved every launch
here to KVM, contrary to this project's no-KVM premise and its acceptance basis.
Snapshots do not record their platform, and restores across platforms, the
guest clock, GPU passthrough and `guest_gs` on KVM are untested. runsc already
ignores `--systrap-disable-syscall-patching` outside systrap
(`runsc/boot/loader.go:1127`). An explicit KVM option would be a separate,
agreed API change.

## Measurements

`scripts/profile-weave-scale.py`, run against an export of 0.2.46 and this
change on `babel-u9-24`, with the client's record counts. Runtime status
queries return immediately, so 0.2.46 admission is a lower bound.

| Measure | 0.2.46 | 0.2.47 |
| --- | ---: | ---: |
| Admission, 17,457 ended and 500 ready records | 684 ms | 24 ms |
| Controller loop pass, 500 live and 3,758 ended allocations | 70 ms | 4.8 ms |
| Name check over those allocations | 482 ms | 0.9 ms |
| `status()` reply | exceeds 4 MiB, fails | 3.34 MB, 550 entries |

Service-network router, 200 frames of 1,242 bytes to a member that stopped
reading: 25.8 s on 0.2.46, 6.8 ms now.

Live, `tests/integration/test_weave_scale_live.py` on `babel-u9-24`: a
controller and two disposable gVisor workers with 2 CPUs and 8 slots each, and
`SANDWEAVE_MAX_STARTING=3`. One unrelated sandbox ran `echo` continuously while
14 more started at once.

| Measure | Value |
| --- | ---: |
| Most sandboxes starting on one worker | 3 (of 14; 486 samples) |
| All 14 ready, including first-use template preparation | 99.8 s |
| Unrelated command, before the burst (median) | 24 ms |
| Unrelated command, during the burst (median / max of 3,331) | 27 ms / 84 ms |
| Killed Sentry seen as failed by the controller | 24.7 s |

Both workers stayed ready in every sample, and every sandbox then ran a
command. This is a small-host check of the coordination changes, not a
reproduction of the client's 424-desktop load.

## Tests

- `tests/test_weave.py`: settled versus uncertain observation, status limit,
  start limit defaults and validation, at most two of five starts at once with a
  limit of 2, ping-based reachability with a 60 s grace, and launch without
  inventory.
- `tests/test_weave_transport.py`: relay priority for `ping` and reply tracking.
- `tests/test_admission.py`: only active records are read; a gone runtime is
  queried once; reservations are still enforced.
- `tests/test_service_network.py`: no router stall for a member that stopped
  reading, and recovery once it reads.
- `tests/test_dashboard.py`: a shared children map gives the same members.

All nine new unit tests fail against 0.2.46 code. The host suite passed: 906
passed, 4 skipped.

The live Weave fixture (`tests/integration/test_weave_live.py`) now also stops
worker instances that its workers start while preparing a template; earlier
runs left them running after teardown. Workers prepared from scratch took over
180 s to start here, so the live run set `SANDWEAVE_WEAVE_STARTUP_TIMEOUT=900`
and `SANDWEAVE_WEAVE_SLOTS=8`.

## Limits

- A ready sandbox whose runtime died keeps its memory reservation on the worker
  until a describe marks it failed. Under Weave that is the next settled
  observation (30 s); a local worker without describes keeps it until the
  sandbox is terminated or its owner expires.
- Out-of-band changes to settled sandboxes, such as a runtime crash, reach the
  controller within about 30 s instead of 2 s.
- Datagrams queued at a service-network member count against the router's one
  send buffer. A member that stops reading (37 frames of 1.2 KB with the
  208 KiB default) makes the router's sends to every group fail until it reads
  again. Pausing a gVisor sandbox pauses its Sentry, not its relay process,
  which keeps reading; a hung or starved relay would still affect other groups.
- `status()` still lists every sandbox that has not ended with its full
  describe result. With entries of the measured 6 KB, more than about 640 live
  sandboxes across the cluster exceed the 4 MiB metadata limit again.
- Worker inventory, telemetry and resource-change detection lag by up to 30 s.
- The client's `WEAVE_MAX_STARTING` setting is not read; use
  `SANDWEAVE_MAX_STARTING`.

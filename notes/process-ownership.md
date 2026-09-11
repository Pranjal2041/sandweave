# Automatic sandbox cleanup

The agreed 2026-09-09 lifetime change is implemented: `Sandbox()` follows the
creating Python process, and `Sandbox(detached=True)` survives that process.
Explicit context cleanup, `terminate()`, and TTL still apply. CLI `create` uses
the detached option. Borrowed handles do not acquire or transfer ownership.

The worker records ownership before starting a guest. Local process identity
includes its PID, start time, host boot and PID namespace; zombie processes
count as exited. The SDK automatically renews one lease per process and worker
every five seconds, and also renews the controller owner when using Weave.
Since 0.2.11, ten minutes without a successful renewal expires ownership permanently.
Brief connection loss can recover within that window; a longer outage can
terminate a remote guest even while the client process is alive.

The worker checks ownership and TTL every 250 ms. Startup checks detect lost
owners between stages and while polling setup. Cleanup skips busy lifecycle
locks and retries failures; detection intervals are not termination deadlines.
Persisted owner records allow checks to resume after worker restart. Cleanup
does not create a checkpoint or remove saved artifacts, and needs a live worker.
Existing records without an owner retain their earlier lifetime.

Ownership is outside the saved template recipe. New snapshot/cache restores
receive their own owner; the detached flag does not invalidate preparation
caches. Forked Python clients register new owners for environments they create.
Closing or dropping a handle does not end the process's ownership.

## Ten-minute grace period

Version 0.2.11 extends remote ownership from 30 to 600 seconds. Both initial
registration and each successful renewal grant the full interval. The controller
uses the same interval when accepting a client and when allowing reconnection
after restart. Renewal remains automatic every five seconds; no application
heartbeat loop is required. Existing worker/controller processes must be
restarted after upgrading to use the new timeout.

The host regression checks survival after 31, 300 and 599 seconds without a
renewal, renewal just before expiry, persistence, and final expiry at the new
deadline. The managed live crash test suspends its test client for 40 seconds,
resumes automatic renewal, then kills that client. It checks that the guest is
still usable 570 seconds later and is subsequently reclaimed. Only local process
identity is withheld; the SDK, HTTP requests, workers and runtime are real, and
the grace period is not shortened for this test.

The installed 0.2.11 candidate survived the 40-second suspension, resumed
automatic renewal, and remained usable 570 seconds after client SIGKILL.
The controller confirmed termination 600.21 seconds after the last renewal;
its saved owner reason was `owner_heartbeat_expired`, and the runtime was stopped.
The test's original final assertion assumed the worker would initiate cleanup.
It now accepts either expiry loop's reason and checks terminated/stopped state;
that corrected assertion was checked against the preserved controller records.
No runtime code changed for this test correction. Logs and the resulting
acceptance report are under ignored `runs/owner-grace-20260911/`.

## Cluster clients returning after an idle period

Version 0.2.5 reused the controller's process-owner ID as the worker lease ID.
After the client released its last sandbox on a worker, there was no worker
route to renew. The worker lease expired after 30 seconds, although the client
was still renewing its controller owner. A subsequent create or pool claim
reused that expired ID and failed with `owner_heartbeat_expired`.
Worker-local PID checks hid this defect in same-host tests.

Workers now persist a mapping from the client's logical owner ID to its current
worker lease. New assignments share that lease while it is active. If it has
expired, a new assignment receives a fresh lease. Existing sandbox records keep
their original lease, so their decided expiry and cleanup are unchanged.
Assignment retries also retain their original lease; they cannot renew an
expired attempt by obtaining a different lease.

Clients still send the same logical owner ID, with one heartbeat per worker.
The worker resolves it to the current lease. No client API or wire change is
required. A still-live legacy lease is adopted when the mapping is first
created, preserving renewal for pre-existing assignments. An expired legacy
lease remains expired. The mapping survives worker restart, and its update is
serialized with lease registration and heartbeats.

The regression cases are in `tests/test_idle_owner.py` and
`tests/integration/test_idle_owner_live.py`. Live tests use disposable workers
and real HTTP/relay requests, withholding the test client's local process
identity to exercise remote lease semantics. They use the configured grace
period without shortening it, checking reacquisition after an idle period,
concurrent new leases, renewal beyond another grace period, and cleanup after
client SIGKILL. The measurements below used the former 30-second grace period.

On 2026-09-11 UTC, the same live idle-pool case failed against unmodified 0.2.5
source (`8de4113`) with `owner_heartbeat_expired`, in 75.30 seconds. With the fix,
all three live cases passed in 317.48 seconds: `warm=0`, `warm=1`, and client
SIGKILL. Both idle cases acquired two sandboxes concurrently after 40 seconds
without a lease, then ran commands successfully after another 35 seconds.
Inspection confirmed that each pair shared a new worker lease, while the
previous lease retained its final expiry. The crash case ended with the
worker recording `owner_heartbeat_expired` and the sandbox terminated.

The unit suite passed 290 tests, with four skips and 101 integration cases
deselected. It includes six new regressions covering the controller/pool
sequence, create and claim retries, legacy records, concurrent registration,
restart persistence and isolation between different owners. Both disposable
clusters stopped, their workers exited, and all acquired guests were
terminated. Existing user clusters and the client's harness were untouched.
Reports without credentials are retained under ignored `runs/idle-owner-20260911/`.

## Validation

- The host suite passed **102 tests**, with one optional test skipped and 60
  integration cases deselected. It covers persisted expiry, PID reuse, zombie
  detection, non-UTF-8 process names, late heartbeats, new restore ownership,
  detached/borrowed contexts, fork registration, independent cleanup locks,
  temporary worker errors and full metadata disks.
- The final installed wheel passed **13 real-runtime ownership cases in
  113.82 seconds**, executed from `/tmp` with the checkout excluded from the
  Python import path. Cases include normal exit, SIGINT, SIGTERM, SIGKILL,
  `os._exit`, closed and borrowed handles, paused and detached guests, death
  during setup with `keep_on_error=True`, pool reserves, native Apptainer and
  GNOME. The remote lease test uses the real heartbeat RPC while deliberately
  withholding local process identity; it is not a separate-host network test.
- **13 additional existing integration cases** passed for pools, CLI lifetime,
  async cancellation, streams, TTL, admission and snapshot-backed descriptors.
  These were run before the final error-handling refinements, whose host tests
  and installed-wheel ownership checks are included above.
- The first normal-exit test initially observed the worker selected before
  first-use installation, while its child used the newly installed runtime.
  The fixture now prepares the template before choosing its observer. The
  corrected case passed in both installed-wheel runs. The original failure is
  retained in `lifecycle.log`.
- All five changed Python modules match their built and installed wheel bytes.
  All **68 task-owned sandbox records** ended in `terminated`. Test fixtures
  shut down their idle workers; the remaining idle worker from the initial
  fixture was also shut down through `_shutdown_if_idle`.
- Both original user desktops remained `ready` with running runtimes. The saved
  data location still points to `/data/user_data/pranjala/sandweave3/.sandweave`.
  No Slurm allocation or gVisor engine source was changed.

Evidence is under ignored `runs/process-ownership-a8cKlj`: `lifecycle.xml`,
`installed-lifecycle.xml`, `installed-final.xml`, their logs,
`wheel-sources.json`, `cleanup.json` and `preserved-desktops.json`.

Run `tests/test_ownership.py` for the host cases. The opt-in integration cases
are in `tests/integration/test_process_ownership.py`; run them only with an
explicit disposable `SANDWEAVE_HOME` and `SANDWEAVE_INTEGRATION=1`.

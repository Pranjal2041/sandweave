# Automatic sandbox cleanup

The agreed 2026-09-09 lifetime change is implemented: `Sandbox()` follows the
creating Python process, and `Sandbox(detached=True)` survives that process.
Explicit context cleanup, `terminate()`, and TTL still apply. CLI `create` uses
the detached option. Borrowed handles do not acquire or transfer ownership.

The worker records ownership before starting a guest. Local process identity
includes its PID, start time, host boot and PID namespace; zombie processes
count as exited. Other clients renew one lease per process and worker every
five seconds. After 30 seconds without renewal, ownership expires permanently.
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

# GNOME startup profiling

The user's `ready_seconds = 34.692398675950244` matched its saved worker record.
A disposable reproduction took 34.13 seconds. Tracing another reproduction
identified a 25.000-second D-Bus activation timeout for `org.freedesktop.Accounts`.
GNOME Shell was waiting for AccountsService, whose systemd unit failed while
creating a private network namespace (`status=225/NETWORK`, `Illegal seek`).

## Where the time went

The traced reproduction took 34.45 seconds:

| Phase | Seconds |
| --- | ---: |
| Runtime launch, systemd and command agent | 3.46 |
| Template setup and diagnostic service | 0.06 |
| X11 window-manager readiness and screenshot connection | 30.11 |
| Session check, resolution and first paint | 0.75 |
| Admission and record overhead | 0.06 |

The 25-second AccountsService wait is **inside** the 30.11-second display phase.
GNOME Shell's own startup unit took 25.96 seconds in the uninstrumented baseline.
The existing launcher's timing file measured roughly 0.3–0.5 seconds preparing
the original containers; it was not the source of the long wait.

## Changes

The GNOME template now configures guest services before executing systemd,
including on cold filesystem restores. AccountsService, hostnamed and localed
use the guest network namespace instead of requesting another private one.
Their AF_UNIX restriction and other unit hardening remain. All three services
successfully activated in the fixed trace. The earlier GNOME Keyring fix also
runs before boot, removing its race with session startup.

TigerVNC's startup script now uses `-nowin` instead of `-iconic`. The clipboard
helper remains running, with its configuration window unmapped. These options
are documented in the [TigerVNC manual](https://tigervnc.org/doc/vncconfig.html).
The template acknowledges GNOME's one-time notice that GDM screen locking is
unavailable in an Xvnc session; other notifications remain enabled.

Readiness waits for GNOME Shell's own startup-complete journal message, then
closes its initial overview through the writable `OverviewActive` D-Bus property.
It waits for the hidden notification before returning. This avoids an early
gray frame and does not enable Shell's unsafe evaluation mode. The upstream
[startup sequence](https://raw.githubusercontent.com/GNOME/gnome-shell/42.9/js/ui/main.js)
and [D-Bus implementation](https://raw.githubusercontent.com/GNOME/gnome-shell/42.9/js/ui/shellDBus.js)
define those signals. Pause/resume and memory restore preserve the user's view.

`env.timings` now includes individual startup phases. Their scopes and overlapping
detail entries are documented in the [usage guide](sdk-usage.md#templates).
Measurements are isolated across worker threads and remain available on failures.

## Measured alternatives

These runs used the existing shared `babel-p9-16` allocation, four advertised
guest CPUs, 8 GiB guest memory, 1 GiB runtime memory, Xvnc at 1280×800 and software
rendering. No GPU or KVM was used. Host load varied; these are observations, not
latency guarantees.

| Operation | Observed time |
| --- | ---: |
| Fresh desktop after the fix, five development runs | 9.15–15.54 s |
| Final fresh-settings run | 10.71 s worker readiness |
| CPU desktop memory restore, two runs | 1.64–1.95 s constructor |
| Ready pool checkout, two runs | 0.048–0.050 ms |
| Ready checkout including first screenshot | 12.98–15.86 ms |

The final fresh run spent 3.84 seconds in runtime/agent startup and 6.84 seconds
in desktop readiness. Its first worker preparation added 18.03 seconds outside
`ready_seconds`; the full constructor took 28.74 seconds. Another run using an
existing worker took 9.39 seconds end to end. Initial dependency installation
and staging took longer still. Reusing a worker avoids repeating that work.

Memory reuse requires a prepared snapshot. The final capture took 2.06 seconds,
then explicit integrity verification took 16.26 seconds. Preparing the one-member
warm pool from that snapshot took 1.69 seconds. Those costs precede checkout.
The pool discards used guests and refills; an empty reserve can require waiting.
These memory-restore results apply to the CPU-rendered desktop. GPU graphics
memory snapshots remain unsupported.

```python
from sandweave import Pool, Sandbox

with Sandbox(template="gnome") as env:
    baseline = env.snapshot(state="memory")
    if baseline.verify()["status"] != "passed":
        raise RuntimeError("Snapshot verification failed")

with Pool(snapshot=baseline, size=1, warm=1) as pool:
    with pool.acquire() as env:
        image = env.desktop.screenshot()
```

## Evidence and reproduction

The profiler creates and terminates its own sandbox. Select a separate data
directory and an existing prepared runtime; use a new output directory each time:

```bash
SANDWEAVE_HOME=/path/to/profiling-data \
SANDWEAVE_ASSETS=/path/to/prepared-runtime \
python scripts/profile-sdk-gnome.py --output runs/my-gnome-profile --reuse --fresh-settings
```

`--trace-bus` additionally records system D-Bus traffic in that disposable guest.
`--fresh-settings` clears only that guest's first-run desktop settings before boot.

Raw timing records, journals, D-Bus traces and actual screenshots are under
`runs/gnome-startup-profile/`. The [measurement record](gnome-startup-measurements.json)
retains the timings and artifact hashes. The final fresh, filesystem-restored,
memory-restored and pool screenshots were opened and inspected. Keyboard input
opened the overview after memory restoration; the saved GNOME Shell PID was
preserved. The desktop integration tests exercise application input, cold
restoration, retained files, and pause/resume with a user-opened overview.

Package verification also exposed a removed template script retained in
setuptools' previous build output. The build now recreates its package output
and rejects a build directory inside the sources. The rebuilt wheel contains
exactly the 105 current package and engine input files, matching their bytes.
Installed in a separate Python environment and run from `/tmp`, that wheel
started a fresh-settings desktop in 9.52 seconds (9.82 seconds for the complete
constructor). Its actual first frame was inspected. The final desktop integration
run passed both tests in 34.68 seconds. Host checks passed 83 tests; one unrelated
VR codec test was skipped because the optional `zstandard` package was absent.
The 47 other integration cases were deselected for that host-only run.

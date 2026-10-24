# Guest clock

Each gVisor sandbox can have its own wall-clock time. Choose an offset from the
worker's clock, or a start date, when you create or restore a sandbox.

```python
from datetime import datetime, timedelta, timezone
from sandweave import Sandbox

# Wall-clock time 400 days in the past.
with Sandbox(clock_offset=timedelta(days=-400)) as env:
    print(env.run("date -u").stdout)

# Wall-clock time that starts at a specific date.
with Sandbox(clock_start=datetime(2025, 3, 1, tzinfo=timezone.utc)) as env:
    print(env.run("date -u").stdout)
```

`clock_offset` takes seconds or a `timedelta`. `clock_start` takes a
timezone-aware `datetime` or an ISO 8601 string such as `"2025-03-01T00:00:00Z"`;
guest time equals that date when the sandbox's runtime starts or restores. Use
one or the other. The CLI takes the same settings:

```bash
sandweave create --clock-offset -2592000 --name thirty-days-ago
sandweave create --clock-start 2025-03-01T00:00:00Z --clock-pause continue
```

`--clock-offset` is in seconds.

## What follows the clock

- The guest's `CLOCK_REALTIME`, through system calls and the fast vDSO path:
  `time()`, `gettimeofday()`, `clock_gettime()`, Python's `time.time()` and `date`.
- Times of files the guest writes in its own filesystem, including `/tmp`.
  Files keep the time at which they were written.
- The boot time in `/proc/stat`.

These stay unchanged:

- `CLOCK_MONOTONIC` and `CLOCK_BOOTTIME` measure real elapsed time, so timers
  and timeouts behave normally.
- Files on host-mounted volumes (`mounts=`) receive the worker's times.
- Worker-side times, such as `ttl`, recording timestamps and `env.timings`.

The guest cannot set its own clock; `settimeofday` and `clock_settime` fail
with `EPERM`.

## Keep sandboxes in agreement

Sandboxes with the same `clock_offset` agree on the time, to within the
agreement of their workers' clocks. A pool applies its offset to every member:

```python
from datetime import timedelta
from sandweave import Pool

with Pool(size=53, warm=8, clock_offset=timedelta(days=-30)) as pool:
    with pool.acquire() as env:
        print(env.run("date -u").stdout)
```

`clock_start` gives each sandbox the same date at its own start. Sandboxes that
start at different moments therefore differ by that interval.

## Pauses

With a clock setting, `pause()` freezes the guest's wall-clock and monotonic
time. After `resume()`, time continues from the paused value: the guest
observes no elapsed time and its timers keep their remaining delays.

| Settings | Guest time during `pause()` |
| --- | --- |
| No clock options | Keeps running |
| `clock_offset` or `clock_start` | Frozen |
| `clock_pause="freeze"` | Frozen |
| `clock_pause="continue"` | Keeps running |

`clock_pause="freeze"` also works without an offset. Each frozen pause delays
the guest clock by the time spent paused. `stop()` and filesystem snapshot
captures pause the sandbox too, so they also freeze time when freezing is on.

CPU sharing never freezes the clock: a throttled sandbox keeps worker-rate
time. A memory snapshot of a running sandbox suspends it briefly while saving;
that suspension does not freeze time.

## Checkpoints and restores

Clock options belong to each start or restore. Caches and snapshots never save
them:

```python
checkpoint = env.snapshot(state="memory")
restored = Sandbox(snapshot=checkpoint, clock_offset=timedelta(days=-30))
```

A restore without clock options uses the worker's time. Processes restored from
a memory snapshot read the new clock, including through the vDSO. Their
monotonic time continues from the snapshot and advances by the real time
elapsed since capture. Saved files keep their times.

Memory snapshots retain the engine that captured them. Those captured before
Sandweave 0.2.45 cannot take clock options; recreate them, or use filesystem
snapshots, which restore with the current engine.

## Inspect the setting

```python
print(env.info["clock"])
# {"offset_seconds": -2592000.0, "pause": "freeze"}
```

A start date appears as `{"start": "2025-03-01T00:00:00+00:00", "pause": "freeze"}`.
`clock` is absent without clock options.

## Limits

- Clock options require `runtime="gvisor"`. Offsets are limited to ±146 years
  and start dates to 1970–2116.
- Guest TLS and other certificate checks use guest time. A large offset can make
  certificates appear expired or not yet valid.
- After a restore with a different offset, `/proc/[pid]/stat` start times of
  processes from the snapshot are relative to the new clock, and no earlier
  than boot.
- Clock options need Sandweave 0.2.45 or newer on the worker and controller.
  Older workers and controllers reject them instead of ignoring them.

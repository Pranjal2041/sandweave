# KVM

gVisor sandboxes run on gVisor's systrap platform by default, which needs no
special host access. On a worker whose user can open `/dev/kvm`, a sandbox can
use gVisor's KVM platform instead: a guest system call then leaves the guest
through hardware virtualization rather than a host signal. No sudo is involved;
the worker only needs read and write access to `/dev/kvm`.

```python
from sandweave import Sandbox

# Require KVM; fails on workers without it.
with Sandbox(template="gnome", kvm=True) as env:
    print(env.info["kvm"])  # True

# Use KVM where the worker has it, the default platform elsewhere.
with Sandbox(kvm="auto") as env:
    print(env.info["kvm"])
```

| `kvm` | Meaning |
| --- | --- |
| `False` (default) | The default platform. A new sandbox never uses KVM unless asked. |
| `True` | KVM. Creation fails with `UnsupportedFeature` on a worker that cannot open `/dev/kvm`. |
| `"auto"` | KVM on workers that can open `/dev/kvm`, the default platform elsewhere. |

`env.info["kvm"]` reports the platform a running sandbox uses. `Pool(...)`
takes the same `kvm` option, and so does a Weave target: Weave places
`kvm=True` sandboxes only on workers that report KVM. The CLI takes `--kvm`,
`--kvm-auto` and `--no-kvm` with `create`, `run` and `pool create`:

```bash
sandweave create --kvm --name fast
sandweave run --kvm-auto -- python3 build.py
```

## Snapshots

A snapshot records the platform its sandbox ran on. A restore that does not set
`kvm` uses the same platform, so a snapshot taken on KVM needs a worker with
KVM. Pass `kvm=False` or `kvm="auto"` to restore it elsewhere:

```python
checkpoint = env.snapshot(state="memory")       # taken on KVM
with Sandbox(snapshot=checkpoint) as again:      # KVM again
    ...
with Sandbox(snapshot=checkpoint, kvm=False) as elsewhere:
    ...                                          # default platform
```

Memory snapshots move between the two platforms in both directions, and their
saved processes keep running. A sandbox restored without KVM records the default
platform for its own snapshots.

## Performance

Measured on one worker (AMD EPYC 7763, Linux 5.14), one sandbox per platform:

| Measure | Default platform | KVM |
| --- | ---: | ---: |
| `getppid` system call | 26.1 µs | 0.80 µs |
| `stat` | 45.7 µs | 2.3 µs |
| `open` and `close` | 68.5 µs | 14.1 µs |
| Pipe round trip between two guest processes | 60.2 µs | 10.9 µs |
| Loopback TCP round trip in the guest | 135.5 µs | 41.9 µs |
| Start `/bin/true` from Python | 6.3 ms | 1.3 ms |
| GNOME desktop ready | 13.5 s | 9.7 s |
| Idle GNOME desktop, host CPU | 0.050 cores | 0.033 cores |
| `env.run("true")`, median of 60 in a row | 17–18 ms | 28 ms |
| `env.run("true")`, slowest 10% | 21–25 ms | 81–105 ms |

Work inside the guest is faster on KVM. A short command sent from Python
through `env.run` was slower and more variable on KVM in these runs; the cause
has not been isolated. With the worker's CPU broker disabled, the KVM median fell
to 16–22 ms, but its slowest 10% stayed at 51–66 ms. Twelve KVM sandboxes
started concurrently were all ready in 2.1 s.

## Limits

- `kvm=True` is not available with GPUs; `kvm="auto"` runs GPU sandboxes on the
  default platform.
- The host must provide hardware virtualization. Cloud VMs without nested
  virtualization have no `/dev/kvm`; where nested KVM exists it has not been
  measured.
- Snapshots taken before Sandweave 0.2.48 ran on the default platform and
  restore there unless `kvm` is set.
- Workers and controllers need Sandweave 0.2.48 or newer. Older ones reject
  `kvm`.

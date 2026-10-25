# Optional KVM platform, 0.2.48

Sandweave runs gVisor on systrap. A client proposed switching to gVisor's KVM
platform automatically whenever `/dev/kvm` is usable; that was left out of
0.2.47 because Babel nodes expose `/dev/kvm` (`crw-rw-rw-`) and every launch
here would have moved to KVM. 0.2.48 adds KVM as an explicit option instead.
The default platform, and the launcher's assertion that `/dev/kvm` is absent
for systrap launches, are unchanged. Raw evidence is in
[kvm-platform.json](kvm-platform.json).

## Agreed API

- `kvm=True` requires KVM; `kvm="auto"` uses it where the worker can open
  `/dev/kvm`; the default is systrap. The CLI takes `--kvm`, `--kvm-auto` and
  `--no-kvm`.
- A snapshot records the platform it ran on. A restore without `kvm` keeps it;
  `kvm=False` or `"auto"` chooses otherwise.
- `env.info["kvm"]` reports the platform in use.

## Implementation

- `run-gvisor.py --platform systrap|kvm` is saved in launch settings. A KVM
  launch passes `--kvm` to `gvisor-host.sh`, which binds `/dev/kvm` into the
  Apptainer container and checks it is a character device; other launches still
  assert it is absent. `--systrap-disable-syscall-patching` is passed only on
  systrap.
- The worker resolves `"auto"` with `os.access('/dev/kvm', R_OK | W_OK)`. GPU
  sandboxes reject `kvm=True`; `"auto"` runs them on systrap.
- Captures stamp the snapshot spec's `kvm` with the platform from launch
  settings. `definition()` inherits it when the restore does not set `kvm`.
- Workers report `kvm` in their inventory and `kvm_platform` in ping. Weave
  places `kvm=True` only on workers whose inventory reports KVM; older workers
  and controllers reject the option.

## Application GS

Firefox 155 crashed on KVM (exit 11, crash-handler minidump) and ran on systrap.
The engine exposes `arch_prctl(ARCH_SET_GS)` only on platforms that keep
application GS, and only systrap without syscall patching declared that.
`sources/gs-preserve-test.c`, which sets the GS base, makes 1,000 system calls
and reads through `%gs`, failed `ARCH_SET_GS` on KVM.

gVisor's ring0 already loads each task's GS base when it switches to user mode,
and KVM has no syscall patches that reserve GS. Fork commit `d2cd4e3` declares
`SupportsGSBase` for KVM. Runtime 2026.10.24.2 contains it and advertises
`kvm_gs`. KVM launches require that capability: prepared engines upgrade for new
launches; a memory snapshot pinned to an older engine restores on systrap under
`"auto"` and fails under `kvm=True`. With the new runtime the probe passed on
both platforms and Firefox 155 produced its headless screenshot on KVM.

## Acceptance

`babel-u9-24`, AMD EPYC 7763, Linux 5.14, runtime 2026.10.24.2:

- `tests/integration/test_kvm_live.py` and `test_kvm_desktop_live.py`: 5 passed.
  Commands, files, DNS, pause and resume on both platforms; `"auto"` selected
  KVM here; memory restores kept KVM or ran without it with `kvm=False`, and the
  saved process kept counting; a filesystem restore kept KVM; the guest clock
  offset matched through system calls and the vDSO; application GS on both
  platforms; twelve concurrent KVM sandboxes ready in 2.1 s; GNOME booted, took
  input and rendered on both platforms. The KVM screenshot was opened and
  inspected. A second desktop run also took Firefox 155 headless screenshots on
  both platforms; idle CPU there was 0.036 and 0.015 cores, so idle figures vary
  between runs.
- Memory snapshots moved between platforms in both directions on runtime
  2026.10.24.1, with the saved counter continuing after each restore.
- The runtime release acceptance (`accept-runtime-release.py`, CLI setup from a
  local candidate) passed before publication. The published archive, manifest
  and checksums were downloaded anonymously and matched.

| Measure | systrap | KVM |
| --- | ---: | ---: |
| `getppid` | 26.1 µs | 0.80 µs |
| `stat` | 45.7 µs | 2.3 µs |
| `open` and `close` | 68.5 µs | 14.1 µs |
| Pipe round trip in the guest* | 60.2 µs | 10.9 µs |
| Loopback TCP round trip in the guest* | 135.5 µs | 41.9 µs |
| Start `/bin/true` from Python* | 6.3 ms | 1.3 ms |
| GNOME ready | 13.5 s | 9.7 s |
| Idle GNOME, host CPU | 0.050 cores | 0.033 cores |
| `env.run("true")`, median back to back* | 17–18 ms | 28 ms |
| `env.run("true")`, slowest 10%* | 21–25 ms | 81–105 ms |

\* Measured on runtime 2026.10.24.1; the GS change does not touch these paths.

## Limits

- Command round trips from Python are slower and more variable on KVM. With
  the CPU broker disabled the KVM median was 16–22 ms and its slowest 10%
  51–66 ms; the cause has not been isolated.
- GPU sandboxes do not use KVM. Nested virtualization (cloud VMs) is untested.
- Only one host was measured; the client's 10.6 versus 1.6 CPU-seconds per
  million system calls on `aws-3` was not reproduced directly.
- Weave places `kvm="auto"` without preferring KVM workers.

# AMS2 splash-screen acceptance and driver stall

September 8, 2026, approximately 08:37–08:56 UTC. **Splash rendering verified;
driving, game frame rate and both-eye capture remain unverified.**

After the [Wine signal repair](wine-signal-context.md), `vr-racing-02` launched
the demo through GE-Proton9-27, DXVK, xrizer, Primus-VK and Monado. The actual
Xvnc screenshot `runs/racing/vr-signal-fixed-later.png` was opened and inspected:
it shows the Reiza Studios splash in an Automobilista 2 Demo window. At this
point four of five prioritized menu/common archives were present; the large
GUITRACKPHOTOS archive completed later, around 08:54 UTC. Full import subsequently
completed around 09:29 UTC; the driver waiter still persisted at 09:31 UTC.

## Observed stall

Shortly after the splash, the test sandbox stopped answering ordinary exec,
FastIO and eye-capture requests. The click's delivery outcome is unknown.
No both-eye image was produced. Guest `RuntimeMaxSec=90` could not stop the run
because guest scheduling itself was stalled. The wrapper collected its final
output only after host-side termination, so an empty log during the hang was
not evidence of no game process.

Host `/proc` inspection identified Sentry thread **147187** in uninterruptible
sleep at `os_acquire_rwlock_write`, executing ioctl **0xc0b8464a** on host FD 78,
`/dev/nvidiactl`. This is `NV_ESC_RM_VID_HEAP_CONTROL`, function 2 (ALLOC_SIZE).
Its parameter structure requested **8,294,400 bytes** of system memory. This
equals a 1920x1080 RGBA buffer and the stall occurred near the eye-capture
request, but that timing/size does not prove the requesting application or
the driver lock's owner. A Go stack RPC timed out; a bounded GDB attachment
also timed out without a usable stack and detached when killed.

Unrelated host vLLM processes were also observed waiting in NVIDIA read locks.
They were not signalled or modified. Kernel logs after test termination report
invalid PAT memtype frees and an NVIDIA system-memory allocation failure.
These logs are later than the initial stall, so they do not establish its cause;
host MemAvailable was about 525 GiB. No GPU reset, driver reload, host sudo,
host configuration change or administrator contact was attempted.

The matching [610.43.02 kernel-interface source](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/610.43.02/kernel-open/nvidia/os-interface.c#L330)
implements the observed wait with `down_write`, an uninterruptible Linux rwsem
acquisition. Its [RM API implementation](https://github.com/NVIDIA/open-gpu-kernel-modules/blob/610.43.02/src/nvidia/src/kernel/rmapi/rmapi.c#L551)
has a global read/write API lock; this is one possible caller, not an identified
lock owner from our evidence. Upstream [issue 968](https://github.com/NVIDIA/open-gpu-kernel-modules/issues/968)
reports the same wait symbol in a different driver/version and initialization
deadlock. Matching the symbol alone does not establish that issue as our cause.

The original Open Saber and Resolve sandboxes continued to answer ordinary
exec. A fresh Open Saber Xvnc screenshot was captured and visually inspected
(`runs/racing/preserved-open-saber-after-stall.png`); it shows the existing song
menu. This single screenshot does not measure continuing GPU frame throughput.

## Test cleanup and remaining host state

The normal `EnvironmentManager.stop(..., discard=True)` request timed out while
forcing runtime deletion. The complete pre-test filesystem snapshot had already
been saved and verified. Sent SIGKILL only to the **348 identity-checked owned
processes** recorded in `runs/racing/stalled-owned-processes.json`, including
the disposable runtime and its helpers. The driver waiter remained: leader
147142 is a zombie and thread 147187 is still in D state as of 08:55 UTC.
Thus cleanup is **not complete**, and `stopped.json` retains `complete:false`.
The ordinary status API uses leader liveness and can report stopped despite
this residual kernel thread; inspecting `/proc/147142/task` is necessary here.
Do not report the runtime as completely reaped until that thread disappears.

Evidence: `runs/racing/vr-signal-fixed.log`, `hang-runtime-stacks.log`,
`hang-gdb-stacks.log`, `stop-stalled-sandbox.log`; guest boot logs remain under
`runs/gvisor/vr-racing-02/`. No changes were made to game executables.

## Scheduler-aware ioctl candidate

The frontend nvproxy helper used `unix.RawSyscall`. A driver wait through that
API retains a Go processor and can block stop-the-world garbage collection.
`scripts/go-blocking-syscall-probe.go` demonstrates the distinction without any
GPU use: a separate OS process releases a pipe read after two seconds. During
the raw call, GC took **1.927 seconds**; with `syscall.Syscall`, GC took
**0.000684 seconds** while the host read was still waiting.

Engine commit `3494ee1` changes the frontend ioctl helper to `unix.Syscall`.
The full build and nvproxy unit tests passed. The candidate is published at
`tools/runtime-builds/ef5a71b09d4cc7c2c1d34dcaf4811f3892c90ea65e0973a0c7093e498f3436bd`.
**GPU-driver recovery and graphical acceptance of this candidate are untested.**
The probe establishes the Go scheduling mechanism, not the NVIDIA lock's cause
or whether this change would prevent that particular driver wait.

`python scripts/stage-gvisor.py --publish-only` now builds an immutable candidate
without selecting it for new launches. Verified that this preserved the default
runtime `19dfb215...`, containing the independently tested Wine signal repair.
Old running desktops continue using their recorded runtimes. Candidate build
and test logs: `runs/racing/nvproxy-scheduled-build.log`,
`nvproxy-scheduled-tests.log`, `go-blocking-syscall-comparison.jsonl`.

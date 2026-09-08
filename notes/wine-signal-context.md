# Wine fault classification in systrap

September 8, 2026. Engine commit `b3ffebb` fixes missing x86 exception metadata
for the systrap path. A real Windows exception-recovery probe now resumes;
this is not yet a racing-game or Alyx gameplay result.

## Trigger and diagnosis

With about half the AMS2 demo's assets imported, DXVK created a 1280x800 FIFO
swapchain and the AVX game executable crashed. Wine reported an unexpected
trap 0 and illegal instruction at `0x141664c00`. Both Wine's debugger and host
objdump identify that instruction as `lock inc DWORD PTR [rcx+0x18]`, with
RCX=0: an ordinary null-pointer memory access, not an unsupported AVX opcode.
Its underlying cause remains unresolved while required archives are missing.
Evidence: `runs/racing/desktop-at50.log`, `desktop-at50.png`.

The pinned gVisor source explicitly left signal-frame Err and Trapno unset,
referencing [upstream issue 159](https://github.com/google/gvisor/issues/159).
[Wine's x86-64 signal handler](https://github.com/wine-mirror/wine/blob/master/dlls/ntdll/unix/signal_x86_64.c)
uses these fields to select its Windows exception path. Trap 0 in the SIGSEGV
handler selects its unexpected-trap fallback, producing illegal instruction.
Page-fault recovery instead requires trap 14 and the write/execute error bits.

`scripts/signal-context-probe.c` reproduced this with the same native Linux
binary outside and inside Sentry. The atomic null write reports signal 11,
trap 14, error 6 on the host; the original sandbox reported signal 11,
trap 0, error 0. Seven other fault scenarios establish the broader ABI gap.

## Engine change and measured limit

The systrap handler now carries raw exception trap/error fields to Sentry.
An optional platform hook records them only after the kernel determines that
the exception must reach the application. Demand paging, CPUID emulation and
other internally resolved faults do not update the application's last exception.
Only an application page fault updates the recorded CR2, using the guest fault
address; unrelated stub addresses are not copied. These fields survive fork
and are included in generated saved state. Live restore acceptance of this
new metadata has not been exercised. Other platforms retain their prior fallback.

The new native/guest comparison matches signal, si_code, trap number and
read/write/execute classification in all eight cases: unmapped read, atomic
write, PROT_NONE read, read-only write, NX execution, UD2, HLT, INT3.
There is **one remaining full-ABI mismatch**: read-only write after mprotect
reports error 6 in systrap versus 7 in native Linux. Sentry unmaps the shadow
mapping when permissions tighten, changing the hardware page-present bit.
Wine uses bits 1 and 4 for access classification, which match. The test's
default strict comparison fails on this discrepancy; the explicit
`--allow-shadow-pte-bit` option permits exactly that known difference and
still reports `exact_linux_match: false`.

```bash
python scripts/test-signal-context.py --allow-shadow-pte-bit
scripts/build-gvisor.sh test //pkg/sentry/platform/systrap:systrap_test
```

Full engine build and systrap unit tests passed. Raw comparison evidence is in
`runs/gvisor/signal-context-1788856040753010680/`; summarized test logs are
`runs/racing/signal-context-test.log`, `signal-context-compatibility-test.log`
and `gvisor-systrap-tests.log`.

## Actual Windows recovery test

`scripts/wine-fault-probe.c` allocates PAGE_NOACCESS memory and installs a
Windows vectored exception handler. The handler checks for a write-access
violation, changes the page to read/write, and returns to the faulting
instruction. A subsequent atomic increment must produce 42.

Compiled with guest `gcc-mingw-w64-x86-64-posix` using `-O0 -Wall -Wextra -Werror`.
The same PE binary and GE-Proton9-27 were run in fresh, dedicated Wine prefixes:

| Sandbox | Engine | Result |
|---|---|---|
| vr-racing-01 | 59487a0 | Exception c000001d, no access parameters; diagnostic exit 10 |
| vr-racing-02 | b3ffebb | Exception c0000005, write operation 1, correct address; resumed=1, value=42, exit 0 |

Full outputs: `runs/racing/wine-fault-comparison.log`. The test is headless
logic; these results do not establish game rendering or gameplay.

`vr-racing-02` was cold-loaded from the complete filesystem snapshot
`snapshots/vr-racing-before-signal-fix` with `--filesystem-runtime-current`.
Save took 4.239 seconds; asynchronous verification passed in 9.249 seconds.
The original runtime and all user-facing desktops remain intact. The new
desktop is on VNC port 45573; its Monado service was explicitly restarted.

The AMS2 probe now reports `service_exit_code`, `crash_reported` and
`novr_argument`. The launcher can exit 0 while AMS2DemoAVX crashes, and `-novr`
has not prevented XR initialization in the observed child. Neither wrapper
exit 0 nor that flag is evidence of gameplay or a verified non-VR control.

# Engineering direction after the first application tests

Objective: maximum practical speed and compatibility without KVM or host sudo,
with full Google Earth Pro, headful Firefox, and Moodle with real nested Docker.
Lab work is independent of the project.

## Establish a repeatable compatibility baseline

Preserve the clean initialized Ubuntu image and application versions. UP UML7.1.3
with the two seccomp fixes and host seccomp=on has exercised all three apps.
Fresh Firefox155 profile now passes actual input as well. This is a functional
baseline, not the final performance target. Repeat full workflows from clean
boots; preserve failures instead of treating a successful retry as proof of a fix.

## Version changes are controlled experiments

Supported Firefox ESR140 passed fresh-profile input on SMP4. ESR153 is still
under test. The presence of a Welcome modal differs between the observed ESR140
and Firefox155 launches; old-profile success can conceal an initialization issue.
Compare fresh profiles, identical memory/graphics settings, and one browser
instance at a time. Keep browser sandboxing enabled. A result that changes with
version identifies a useful compatibility choice, not necessarily the buggy layer.

An older LTS UML kernel is a reasonable subsequent regression control, with
matching required Docker/network features and the same userspace. Do not assume
it removes the seccomp metadata defects or improves speed. Disabling SMP in the
current source is a more focused initial control than changing several versions.

## Isolate failures before modifying the engine

The two demonstrated seccomp ABI defects have standalone before/after tests.
The clock_gettime/EFAULT abort and later SMP fresh-profile hangs have no isolated
reproducer yet. Clock/signal/mmap stress passed native, UP and SMP4; it did not
explain Firefox. Capture signal context, guest pointers, page mappings and memory
copy failure where the real fault occurs. Use a small reproducer and regression
test to justify any additional kernel patch. Pinning Firefox to one guest CPU
has not been demonstrated to resolve the earlier hang.

## Optimize application bottlenecks

The raw-getpid benchmark was ~.246us native versus ~10.412us UML. It measures
kernel crossing cost, not a 42x application slowdown. Native arithmetic executes
near host speed. Four threads inside one process did not scale, whereas four
processes did; UML's current Kconfig explicitly documents this restriction.

Collect host wall-clock distributions for browser input/navigation, real Moodle
login/course operations, and Google Earth cached camera navigation and imagery
loading. Separate initial downloads, server response, rendering and artificial
test sleeps. Compare software rendering with the same resolution and worker count
before evaluating extra native rendering threads. Native application controls
are useful bounds but do not establish a full privileged-environment replacement.

Candidate engine work, conditional on those profiles:
- Clock fast path: UML's x86 vDSO currently traps every clock read. A proper guest
  shared clock page could remove frequent transitions. Preserve clock IDs,
  monotonicity, guest offsets, adjustments and syscall fallback. This is a design
  proposal, not an implemented fix or an explanation of EFAULT.
- Syscall and I/O paths: compare supported execution modes, mmap/futex overhead,
  disk staging, writeback and network copies under actual workloads.
- True parallel application threads: requires changing UML's per-address-space
  stub/turnstile design and correctly synchronizing mappings, registers, signals,
  futexes and thread lifetime. This is substantial engine work, not a CPU flag.

Continue preserving the full guest kernel, services, Docker bridge/NAT and nested
MariaDB. A native or alternative backend should only replace that configuration
if it passes the same full workflows and required privilege/storage semantics.
The current evidence does not establish maximum speed, universal Linux support,
or compatibility on a second physical node that lacks KVM.

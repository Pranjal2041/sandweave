# Active objective

## Latest user steering — supersedes the historical run status below

The user subsequently said "yes, so work on it", authorizing implementation and
experiments of the proposed gVisor-derived engine in this independent lab. UML
remains paused. Current results are in `notes/gvisor-prototype-progress.md`.
The read-only investigation statement below describes the preceding research
turn, not this implementation round. Do not restart historical UML jobs.

UML is paused by explicit user instruction. Investigate other architectures that
preserve the full environments without KVM, host sudo, or administrator changes.
Do not automatically resume the historical browser/graphics experiment.

The current investigation is recorded in
`notes/no-kvm-engine-investigation-2026-09-06.md`: actual Slurm/host constraints,
upstream source and PR review, a concrete userspace-kernel candidate, remaining
engineering gaps, and independent acceptance gates. No new VM/application/build
or benchmark was launched during that investigation. No project files changed.

The goal tool reported the persistent goal as paused. The historical process IDs
and statements that a run is active below were not revalidated this round.

## Historical objective and experiment status

Maximize practical speed and compatibility without KVM or host sudo on this
Slurm cluster. Required standards: Google Earth Pro, headful Firefox, and Moodle
with actual Docker nesting and normal service/network behavior preserved.
Experiments remain independent under ~/scratch/general-vm; project files may be
read for requirements but project runtime/scripts are not used.

Completion requires visible application interactions, persistence and nested
Docker behavior, repeatability, host wall-clock measurements and comparison of
viable configurations. Microbenchmarks alone do not establish application speed.

Current evidence is in notes/ptrace-application-progress.md and
notes/browser-version-comparison.md. notes/engine-direction.md records the
engineering plan and which proposed optimizations remain unimplemented.

A working single-CPU application baseline now exists: UML7.1.3 compiled UP,
both seccomp ABI fixes, host seccomp=on, full Ubuntu22.04, Firefox155 sandboxed,
Earth7.3.7, Moodle4.5/Apache/PHP and actual inner Docker MariaDB. Clean initialized
image images/ubuntu-uml-ready.ext4, SHA256 in notes/ready-image-sha256.txt.

Latest comparison: SMP4 fresh-profile Firefox140.15.0esr and153.2.0esr both
passed three real input submissions with sandboxing. Fresh155 passed on UP.
SMP current155 follow-up ended with SIGKILL, sender unknown; this is inconclusive
about cause. Full evidence and failed attempts in browser-version-comparison.md.
SMP run replay-20260906T204442Z-2959339 shut down cleanly, launcher exit0. Earlier two VMs
exited143 unexpectedly; Slurm remained healthy, task cgroup had no OOM events.
Their abrupt raw disks are not checkpoints.

Outstanding: repeat all three full application workflows on the strongest
candidate configurations; measure host wall-clock application speed against
appropriate controls; isolate intermittent clock/EFAULT/hang symptoms and record
termination senders; evaluate bottleneck-driven engine work. ESR compatibility
success does not remove syscall costs or within-process thread serialization.
Goal is not complete.

Current graphics round: notes/graphics-performance-progress.md is authoritative.
UML SMP4 replay-20260906T212538Z-3666975 active on22022/25901, launcher97658.
Native LP4 Earth was ~4x faster in delivered VNC updates than LP1. Testing
UML baseline, LP0 thread-handoff avoidance, and a host software-rendering service.
Virgl0.9.1 probes failed; upstream1.3.0 build in progress. Goal remains active.

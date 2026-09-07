# Browser version and fresh-profile comparison, 2026-09-06

All runs use the clean checkpoint images/ubuntu-uml-ready.ext4, the same two
experimental UML 7.1.3 seccomp fixes, host-side seccomp=on, 4GiB RAM, llvmpipe
LP_NUM_THREADS=1, and Firefox's normal sandbox. No project runtime is used.

- SMP4 run replay-20260906T203539Z-2785481: Firefox140.15.0esr, newly created
  profile firefox-fresh-esr140, passes three real input/HTTP checks. Corrected
  harness exits0 in esr140-input-retry.log; ten content processes in the actual
  browser service cgroup report Seccomp2. esr140-input-pass.png visually inspected.
- UP run replay-20260906T203612Z-2799717: Firefox155.0.1, newly created profile
  firefox-fresh-up155, Welcome Continue clicked normally. Three real input/HTTP
  checks pass; corrected harness exits0 in fresh155-input-retry.log; ten content
  processes report Seccomp2. Screenshot input and result match. Screenshot caught
  some partially repainted static page text, so do not claim smooth rendering.
- Neither completed input run reported clock_gettime failure or segfault in the
  queried guest journal. This does not prove the earlier failure is fixed.
- ESR140 has no equivalent welcome modal in this observed fresh-profile launch;
  this is a confound when attributing results to browser version or UML SMP.
- First harness attempts failed after some passing trials because address-bar
  focus changes had not finished before typing. Screenshots show partial URLs
  and URL text in the page input. Added explicit focus waits, slower real key
  entry, observed navigation HTTP request before page input, and input clearing.
  Keep original failed logs. Harness duration is not a performance benchmark.
- ESR153 extracted and launch accepted by systemd, but no UI/input result yet.
  Both launchers then exited143 (SIGTERM); ports closed at ~20:43UTC. No guest
  shutdown or kernel panic in console. Host Slurm job10333558 remains RUNNING
  with >43h left; task cgroup reports no OOM events. Signal origin unknown.
- Abruptly stopped raw disk is not a checkpoint. Read-only fsck on SMP raw disk
  skipped journal recovery and reported inconsistencies; this does not establish
  corruption after journal recovery. The clean ready checkpoint remains intact.
- Next run should start from the clean ready image, not the abruptly stopped raw
  disk. Compare ESR153 and current155 on SMP4 with fresh profiles sequentially.

Mozilla official product metadata lists ESR140.15.0esr, ESR_NEXT153.2.0esr and
current155.0.1. Archives checked against the corresponding Mozilla SHA256SUMS.
https://product-details.mozilla.org/1.0/firefox_versions.json

None of these results remove UML's documented within-process thread serialization
or its syscall interception costs. Older Firefox is a compatibility candidate;
an older kernel remains untested, and no global speed optimum is established.

## ESR153 result after a clean restart

New SMP4 run replay-20260906T204442Z-2959339: Firefox153.2.0esr with new profile
firefox-fresh-esr153. Welcome Continue clicked normally, maximized via title bar.
Three input submissions pass; esr153-input.log exits0, nine content processes
in the actual service cgroup report Seccomp2, no queried journal fault entries.
Screenshot after Continue visually inspected. This launch used a TTY tool session;
no observed termination so far. Current155 fresh-profile comparison follows in
this same guest with ESR fully stopped; do not attribute improvement to ESR alone.

## Current155 follow-up is inconclusive about cause

Same SMP guest after stopping ESR153: fresh profile firefox-fresh-smp155-round2,
mainPID6334. Welcome visible and Continue clicked. No input callback completed;
input harness exited1. Later screenshot shows no browser, and full journal records
at20:51:11 main process code=killed,status=9/KILL, unit result=signal. Children
report channel EOF. Sender of SIGKILL is unknown; do not call this a spontaneous
Firefox crash, a proven UML bug, or proof ESR fixes the cause. A late systemctl
show on the collected transient unit returned success/default fields; the full
journal is the authoritative exit evidence. The earlier UP155 input pass remains.

Artifacts: fresh155-ready.png/log, fresh155-input.log, fresh155-during-input.png/log,
fresh155-exit-diagnosis.log, fresh155-user-journal.log. ESR153-input-pass.png was
opened and inspected and shows the matching submitted token. Some screenshots
captured partially repainted text; smooth rendering/latency is not established.

Shutting down this VM cleanly at20:54UTC. Keep its raw disk as an experimental
run; the durable clean ready image remains the application baseline. Any next
failure investigation should record guest kill/tkill/tgkill sender/target and
host termination context, alongside the existing clock/signal diagnostics.

Shutdown completed normally: launcher45364 exited0, console reached Power down,
and SSH/VNC ports22022/22023/25901/25903 are all closed. Shutdown diagnostics did
not contain OOM/out-of-memory/killed-process entries. This still does not identify
the Firefox SIGKILL sender. No additional kernel patch was applied in this round.

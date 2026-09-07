# UP / ptrace application comparison, 2026-09-06

Current run: runs/replay-20260906T192224Z-1430540. Same two seccomp patches,
Linux 7.1.3 compiled CONFIG_SMP=n, 1 guest CPU, host execution seccomp=off
(ptrace). This does not disable Firefox's guest seccomp sandbox.

Headful Firefox 155.0.1 on GNOME accepted typed input and submitted it to Docker
nginx. Three unique inputs were observed as actual HTTP requests; repeated
again successfully. Screenshots were opened and visually inspected. Content
processes report NoNewPrivs=1 / Seccomp=2. No clock_gettime failure in the journal.
The first two test script exit codes were 1 due to harness checks: ps -C firefox
(the process name is firefox-bin), then journalctl returning 1 for no matches.
These errors are fixed in scripts/test-firefox-headful-input.sh; retain original
logs as evidence and rerun the final harness before reporting its exit status.

Native headful Firefox control, using the extracted guest filesystem and host
kernel with /dev/kvm hidden, also accepted input and submitted the page. Visually
verified runs/native-control/input-diagnosis.png. Its initial automated X window
name check failed despite visible success (no window manager in this control).
The native control ended at its configured 600-second timeout, exit 124.

Google Earth Pro 7.3.7.1327-r0 installed. Its postinst required xdg-utils, installed
successfully. GNOME + llvmpipe, LP_NUM_THREADS=1. Startup tips and update notice
dismissed with real clicks; rendered globe with textures, country boundaries,
and navigation controls visually inspected in earth-after-notice.png. Navigation
and persistent placemark test pending.

Moodle build in progress. Independent Ubuntu 22.04 image with systemd,
Apache/PHP 8.1, Moodle 4.5.13, and Docker. Planned MariaDB 10.11 is an actual
inner Docker container; volumes preserve database, Moodle data, and config.
No runtime or scripts from the project are executed or copied into this lab.

## Further results

Final ptrace Firefox harness exits 0: headful-input-final.log, three input/HTTP
checks and ten sandboxed content processes. Google Earth actual coordinate search
loaded Golden Gate imagery; UI-created placemark saved with correct coordinates
on closing via window close. Ctrl+Q did not close Earth and was not counted as
persistence. earth-placemark-saved.log proves parsed KML, and screenshots show
actual rendered bridge and named placemark. Native Earth control achieved the
same search and saved-placemark check after adding Metacity for window management.
Native screenshots/logs under runs/native-control. Native Firefox control used no
window manager; these controls are application controls, not a full VM substitute.

Second guest, UP compiled kernel with seccomp=on, SSH22023/VNC25903:
runs/replay-20260906T194250Z-1812148. Same kernel binary as ptrace. Three headful
input checks pass, exit0, sandbox active; screenshot input-3.png inspected.
Stopped cleanly, launcher92352 exited0, raw disk retained. This proves the host
seccomp mode alone does not inevitably cause Firefox failure. Third guest uses
that stopped filesystem with the SMP kernel and ncpus=1, seccomp=on:
runs/replay-20260906T195126Z-1968627. Firefox initially slower to show but currently
renders a normal page. Input check pending; no proven SMP root cause yet.

Moodle image built successfully. Real outer systemd and Docker, inner MariaDB.
Two lab setup defects found and fixed in scripts:
1. Ubuntu Docker29 uses separate containerd /var/lib/containerd. This must have a
   persistent ext4-backed volume as well as /var/lib/docker. Without it, creating
   a whiteout node on outer OverlayFS failed EPERM. Identical mknod succeeds on
   ext4; adding the volume allowed MariaDB pull and startup with normal networking.
2. Lab PHP ini COPY retained host mode0640 root:root. Nonroot php -v segfaulted,
   while root and php -n worked. Changing the non-secret ini to0644 makes nonroot
   php -v work. All individual extensions also worked. This is a lab permissions
   defect; do not attribute it to UML. Dockerfile and startup correct the modes.
Moodle CLI installation is now running; database was empty before this attempt.

SMP build with ncpus=1 also passes the same three Firefox input checks with
sandboxing; runs/replay-20260906T195126Z-1968627/headful-input-test.log exits0.
It and the primary ptrace guest shut down cleanly, launcher exit0. Original
four-CPU failure remains unresolved; no claim that SMP compile alone causes it.

Moodle installer generated config.php with a relative __DIR__ require which
fails when config.php is a symlink into /etc/moodle. Lab startup now provides
an explicit standard Moodle configuration (dirroot=/var/www/html/moodle) and
uses Moodle's official install_database.php CLI. Database remains empty before
this corrected attempt. This is another lab setup fix, not a UML bug.

Primary raw disk passed e2fsck -fn with no errors and is being checkpointed as
images/ubuntu-uml-apps.ext4. This checkpoint contains installed apps, normal
nested Docker, empty Moodle schema, Google Earth saved placemark, and Firefox
profile. Do not count Moodle installation or interactive Moodle as completed.
Launchers now allow independent SSH/VNC ports and use immutable digest-named
base caches so different concurrent images cannot overwrite COW backing files.

## 20:20 UTC state

Primary current guest: runs/replay-20260906T200146Z-2154447, SSH22022/VNC25901,
UP kernel seccomp=on, 1 CPU. Launcher97181 has 5400s timeout from ~20:01 UTC.
Moodle successfully installed with 494 tables. Headful Firefox admin login
succeeded; UI created course id2 / NOKVM101 / No KVM Integration Course.
scripts/test-moodle-persistence.sh exits0: same DB row survives outer container
restart, systemd Apache/Docker/containerd active, login HTTP serves normally.
Screenshot moodle-after-restart.png shows the course after browser hard reload.
HTTP host wall-clock login-page timing: first .564s, warm median .09764s, max
.2794s over nine warm requests. Includes local SSH forwarding (session72862,
port28082), excludes browser rendering. Not an end-to-end application comparison.

Four-CPU comparison: runs/replay-20260906T200141Z-2154691 SSH22023/VNC25903,
SMP kernel seccomp=on, 4 CPUs. Launcher52983 timeout1800s from ~20:01 UTC.
Saved Firefox profile passes three input/HTTP checks with sandboxing (retry log).
Fresh profile firefox-fresh-smp4 hangs at welcome, GNOME reports not responding,
mainPID6862 used ~323% lifetime CPU and was seen in D/R state. No clock EFAULT
journal message in this run. Thread stacks preserved. Pinning after the hang did
not establish recovery; process killed via its user systemd service, confirmed
MainPID0 and PID absent. A new fresh profile firefox-fresh-pinned-smp4 is now
launched with taskset -c3 from the beginning. Result still pending.

Harness improvements: allow initial window activation retry, Escape to exit
GNOME overview, unique cache-busting URL per input trial. Real HTTP input callback
and inspected screenshot remain the success evidence, not X window title alone.
Some early window-focus attempts typed into GNOME search; those do not pass.

Clock/signal stress program scripts/clock-stress.c builds tools/clock-stress,
16 pthreads alternating raw/libc clock_gettime with guarded stack outputs,
periodic mmap/munmap and SIGUSR1 clock calls. Native passes. Current 10k/thread
runs on primary UP (session14508) and secondary SMP CPUs0-2 (session18539),
90-second in-guest timeouts; collect statuses before changing/restarting them.
Native Earth and Firefox control processes have ended; native Earth saved its
UI-created placemark at correct coordinates (earth-placemark-pass.log).

## 20:29 UTC update

Clock/signal stress passes on native (20k/thread), UP seccomp and SMP4 seccomp
(10k/thread), zero failures. This did not reproduce Firefox's fault.
Fresh Firefox155 pinned to guestCPU3 from launch also failed input: remained
unresponsive, ~99% lifetime CPU. Affinity is not an established workaround.
SMP4 secondary stopped cleanly, launcher52983 exits0. Raw disk/logs retained.

Primary Google Earth on UP seccomp reopened the previously saved placemark
from the checkpoint; real double-click navigated to it. Screenshot
 earth-replay-placemark.png inspected. Thus UP seccomp has exercised Firefox155,
Google Earth, and complete Moodle with actual Docker nesting and UI-created
persistent course. Full performance/compatibility comparisons remain incomplete.
Primary is now shutting down cleanly to save the fully initialized state.

Next comparisons: supported official Firefox140.15.0esr and153.2.0esr, alongside
155.0.1, each with fresh profiles on SMP4. Archives saved in downloads and
validated against Mozilla SHA256SUMS. Mozilla product-details lists ESR140.15
and ESR_NEXT153.2 as of Sep6. No application has been replaced by a lighter UI.

# Graphics performance experiments, 2026-09-06

All work remains independent of the project. Previous goal turn made progress:
fresh-profile ESR140 and ESR153 passed headful input on SMP4; current155 SMP exit
was SIGKILL from an unknown sender. This round measures application performance.

Host-native Earth7.3.7 uses the same extracted Ubuntu22.04 filesystem under
Apptainer --userns --containall, KVM hidden, TigerVNC1280x800 and Metacity. CPU
allowance is pinned to0-7. This is an application control, not a complete
privileged environment substitute. Native WM differs from guest GNOME.

Fixed native VNC auth path: /home/ga is hidden by Apptainer, so use disposable
/session/vnc.passwd instead. Native Earth launcher now starts Metacity under the
same D-Bus session. Source scripts and dependency versions are retained.

An early native close attempt used xdotool windowclose and produced a heap
allocator message; this was not spontaneous during navigation. It required
TERM of the known Earth PID325... predecessor. The subsequent run closed
normally through the actual window close button, launcher88446 exited0, no
heap message. Do not attribute the earlier forced-X-window-close error to UML.
Original log: runs/native-control/earth-native-close-heap-error.log.

Google Earth reopened its real saved Golden Gate placemark at range1000m,
loaded detailed imagery, and accepted repeated mouse pans. Update notification
layer was unchecked and startup tips disabled through normal UI controls.
Screenshots of baseline and mid-pan were opened and visually inspected.

scripts/measure-vnc-drag.py measures host wall-clock visual updates delivered
through VNC using incremental RAW (or ZRLE) encoding, cursor suppressed, same
700x520 ROI, six-second sinusoidal real mouse drag at20 input events/sec. It
records actual input schedule delays, received bytes, image change fractions,
and full screenshots outside the timing loop. Counts are delivered viewport
updates, not renderer FPS. End-to-end includes input, rendering, VNC server,
transport and client processing. First visible change threshold2% of sampled ROI;
changed-update count requires1% change. Download/init time excluded by first
opening the saved view and waiting; each repeat resets to that placemark.

Native LP_NUM_THREADS=1, three RAW trials: median first visible change0.981s,
median changed viewport updates3.025/s. Actual input scheduling jitter<2ms.
Data and images: runs/visual-bench/native-lp1-raw-{1,2,3}/.
This shows software rendering/transport already has substantial cost natively;
it is not a UML speed comparison. LP4 and UML comparisons follow.

Potential optimizations to evaluate:
- Mesa documents LP_NUM_THREADS=0 disables worker threading entirely. Avoiding
  rasterizer handoffs may suit UML's serialized application threads. Not tested.
- Native indirect GLX probe failed context creation with existing VNC settings.
- Mesa virpipe/vtest can send rendering to a separate process. Source23.2.1
  inspected from official GitLab under sources/graphics-inspection. Protocol>=2
  passes shared-memory FDs, which cannot cross the guest/host kernel boundary
  through a plain TCP relay. Legacy protocol0 uses socket transfers and may allow
  a relay; needs an explicit protocol cap and real compatibility/performance test.
  This is a possible graphics-driver change, preserving guest apps and services,
  not an implemented working solution. No claim of GPU access or acceleration.

## Native four-worker result

Three valid LP4 RAW trials: median first visible change0.1566s and median
11.933 changed viewport updates/s, versus LP1 medians0.9812s and3.0249/s.
Same saved view, application, Mesa version, CPU allowance0-7 and VNC geometry.
LP4 process environment and affinity checked. One earlier LP4 attempt was covered
by startup tips; it returned insufficient visible movement and is excluded.
Valid data: native-lp4-raw-verified-{1,2,3}. Baseline and mid-pan screenshots opened.
A reference-image guard now rejects a substantially different initial viewport.
Native Earth closed normally through its close button, launcher18441 exited0.

SMP4 UML current run replay-20260906T212538Z-3666975, SSH22022/VNC25901,
launcher97658 (TTY, timeout5400s from21:25UTC), pinned host CPUs0-7, clean ready
image. Earth LP1 launched with new scripts/start-earth-gui.sh. Baseline comparison
pending; no project runtime used.

Rendering-service exploration: Ubuntu virgl-server0.9.1 and libs extracted only
into the lab native filesystem. Default vtest protocol2 GLX probe failed XGetImage
BadMatch. scripts/vtest-relay.py forces negotiated protocol0 (no shared-memory FD
passing) over a TCP/Unix relay. Local protocol0 probe reached the renderer, but
old renderer child segfaulted. Thus no functioning renderer proxy is established.
Server59616 is still listening with --use-egl-surfaceless, LP_NUM_THREADS4,
software llvmpipe and KVM hidden. Host relay71484 binds127.0.0.1:19500; local
probe relay45720 uses runs/native-control/tmp/virpipe-legacy.sock. Source1.3.0
from official GitLab saved/extracted; building with host GCC and Ubuntu dev
packages in the lab native-root. First Meson setup lacked x11.pc; fetching those
header packages. Build workers restricted to CPUs8-13, away from UML benchmark.

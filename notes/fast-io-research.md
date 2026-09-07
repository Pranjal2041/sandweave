# Fast actions and screenshots for the no-KVM desktop

Research dated 2026-09-07. No environments, benchmarks, installers or builds were
started for this investigation. The current desktops and engine were preserved.
This is a proposed design with source evidence and earlier measurements, not a
claim that fast I/O has already been implemented or timed in this lab.

The recommended starting point is Gym Anything's existing native C fast-I/O
service: persistent XTest input, MIT-SHM capture, and a buffered frame cache.
Adapt its transport and delivery contract to our gVisor desktop. A roughly 10 ms
local action/screenshot target has credible supporting evidence. A bound on an
application repaint, remote Mac round trip, or a CPU-throttled guest does not.

**Code inspected and the existing contract**

The Gym Anything working checkout is an older evaluation branch with unrelated
changes. It was not edited or switched. The fast-I/O audit used local Git blobs
at `origin/main`, commit `91bcbd77733ff1e1d73e5a07445aa1ad711c15e3`, including:

- `src/gym_anything/env.py`, particularly `step`, `_action_gap_seconds` and
  `_capture_observation`.
- `runtime/runners/qemu_dbus_display.py` and `qemu_apptainer.py`, below
  `src/gym_anything/`.
- `runtime/runners/linux_uinput_fast_inputd.py` and the keyboard, pointer and
  action-gap conformance tests.
- `runtime/runners/modal_native_assets/fast_io_server.c`,
  `runtime/runners/modal_native_fast_io.py`, `modal_native.py`, `base.py`, and
  `tests/test_modal_native_runner.py`.

The audited QEMU screenshot listener receives D-Bus `Scanout` and `Update`
payloads, keeps a BGRX bytearray, copies it and converts it to a PIL RGB image.
It reads the latest received frame; it does not force a repaint. This is an
in-memory path, but this implementation does not map the guest framebuffer
directly. Upstream QEMU also exposes a Unix mapping interface that this listener
does not implement. See the [audited listener](https://github.com/cmu-l3/gym-anything/blob/91bcbd77733ff1e1d73e5a07445aa1ad711c15e3/src/gym_anything/runtime/runners/qemu_dbus_display.py)
and [QEMU's display protocol](https://www.qemu.org/docs/master/interop/dbus-display.html).

Input evolved beyond simply acknowledging a QEMU command. The Linux path now
sends complete gestures to an in-guest uinput daemon and checks X-server
pointer/button state, keyboard state and raw-event counts. Earlier queue-only
acknowledgments allowed modifiers, repeated clicks and drag waypoints to be
misordered or collapsed. Host round trips for every transition were also costly;
confirmation beside the device was much cheaper. This history is documented in
[PR 53](https://github.com/cmu-l3/gym-anything/pull/53). Its conformance guarantees
should carry over; its uinput implementation should not be copied blindly into
gVisor, whose inspected device implementation has no uinput support.

`fast_io=True` returns a PIL image directly and removes the usual post-action
and step-cycle waits. The separate `capture_screenshot(path)` method still
encodes PNG. Audio and UI-tree observations can add their own synchronous work.
A 10 ms RGB observation claim must name which API and observations it includes.

There is a specific integration gap in the audited native runner:
`BaseRunner.acks_input_delivery()` returns false, and `ModalNativeRunner` does
not override it. Consequently, `env.step` can retain its default 200 ms gap
between actions even though the C server replies after `XSync`. A gVisor runner
must advertise the capability only after its delivery contract passes tests.

**Existing measurements that make this worth pursuing**

[PR 50](https://github.com/cmu-l3/gym-anything/pull/50) reports these results at
1920x1080 inside its Modal VM, using at least four vCPUs:

| Operation | p50 | p95 |
|---|---:|---:|
| Changed screenshot to isolated RGB PIL object | 1.98 ms | 2.28 ms |
| Cached screenshot | 0.55 ms | 0.57 ms |
| Input round trip | 0.044 ms | 0.050 ms |
| Native server capture | 1.28 ms | 1.50 ms |

These are previous author-reported measurements, retrieved from the PR body
through the GitHub API, not measurements repeated here. They do not include our
gVisor boundary or prove that the next application frame contains an action's
effect. Capture service time and cached-frame retrieval can overlap; adding
their medians does not produce an end-to-end action-to-repaint latency.

The existing service has persistent connections, batched XTest input, XDamage
notifications, MIT-SHM capture, three RGB buffers and a seqlock for local reads.
It also has a raw-RGB socket path. Its colocated mmap path assumes access to the
same `/dev/shm` file, which the host does not automatically have inside gVisor.

**Proposed input path**

Keep one helper inside each environment, connected to its X server. Send a
complete gesture as one request; preserve transition order locally. XTest
injects through the X server's input machinery without exposing host
`/dev/uinput`. The examined X-server handler calls `mieqProcessDeviceEvent`
while handling fake input. An X round trip after injection therefore provides
a much more direct server-processing barrier than a virtual-device enqueue.
See the [XTEST protocol](https://www.x.org/releases/X11R7.7/doc/xextproto/xtest.html)
and [X-server implementation](https://github.com/XQuartz/xorg-server/blob/master/Xext/xtest.c).

Still validate asynchronous X errors, held state, event counts and ordering.
An XTest function's success return alone is insufficient. Input frozen by a
grab needs an explicit outcome; a final all-keys-up state cannot prove that
every requested press occurred. Reuse the existing conformance scenarios for
modifiers, repeated clicks, wheel events, drags, Unicode and grabs. Filter
observations to the injected device/sequence where possible, since an active
VNC user can also produce raw events. The installed Xvnc version must be tested;
reading another X-server revision is supporting evidence, not that test.

Return an environment generation, action sequence, acknowledgment level, and
server timing. The ordinary success level means input processed by the X
server, not application handler finished or pixels repainted. A disconnect
after dispatch must report an unknown outcome unless a sequence-based result
lookup resolves it; never automatically repeat a possibly executed click.
Games that bypass X11 input require separate qualification.

**Proposed screenshot path, including the gVisor boundary**

Our current graphics route is already documented in [single-GPU setup](single-gpu.md):
VirtualGL renders through NVIDIA EGL and transfers the result into the guest
Xvnc desktop. Software rendering reaches that same desktop. MIT-SHM capture of
the final root framebuffer therefore has a common target for both. It should
not require an additional GPU readback solely to capture a screenshot. This
reasoning is specific to the current Xvnc/VirtualGL desktop, not arbitrary
Wayland, DRM scanout or hardware-overlay configurations.

Start from the existing helper to establish a baseline. The preferable bulk
transport to test is a host-created `memfd` with bounded frame slots, donated
to the helper using `runsc run --pass-fd`. The host maps its own descriptor;
the guest maps the donated descriptor. This shares deliberately allocated
pixel storage, not the guest's general memory or host X display.

There is a standard way to remove another full-frame copy: use MIT-SHM 1.2
`xcb_shm_attach_fd` to pass the donated descriptor to Xvnc over its local Unix
socket, then `xcb_shm_get_image` to capture into a selected slot offset. The
X server can write native-format pixels into that backing memory; the host can
then convert into an owned RGB image when required. These operations are in
the [XCB SHM API](https://xcb.freedesktop.org/manual/group__XCB__Shm__API.html).
The examined [X-server SHM handler](https://github.com/XQuartz/xorg-server/blob/master/Xext/shm.c)
maps attached descriptors with `MAP_SHARED`.

Source evidence from our engine, commit
`59487a05f5e858d5b20a36d980036ccea2ad82ab`:

- `runsc/cmd/run.go` accepts repeated host-to-guest descriptor mappings.
- `runsc/boot/loader.go:createFDTable` imports passed files as restorable.
- `pkg/sentry/fsimpl/host/host.go:ConfigureMMap` allows regular files; its
  translation uses the host-backed mapping.
- `pkg/sentry/socket/control/control.go:NewSCMRights` passes file descriptions
  between guest Unix-socket endpoints. The helper-to-Xvnc transfer stays inside
  the guest; it does not depend on arbitrary host-socket SCM_RIGHTS import.

Together these make direct capture into a host-visible memfd a concrete
experiment. It remains untested in this exact combination. Verify MIT-SHM 1.2
and descriptor attachment in the installed Xvnc, shared visibility, image
format/stride, stable publication and errors. If descriptor attachment fails,
ordinary guest SysV MIT-SHM followed by one copy into the donated ring is a
reasonable fallback with the same API.

Use persistent small control messages, initially over the existing forwarded
connection for comparison. Also measure a pair of donated request/reply pipes
to bypass packet forwarding entirely. Neither option requires host sudo or
KVM. Do not assume a guest futex and a host futex on shared bytes share the
same wait queue. A control channel plus bounded publication protocol avoids
that dependency. Direct host Unix sockets have distinct checkpoint and ancillary
data constraints, so they should not be substituted without qualification.

Publish a slot only after the image reply confirms completion. Protect slot
reuse while copying, validate bounded metadata and use a new generation on
restart. Returning an owned PIL image requires a copy/conversion; this is not
an end-to-end zero-copy claim. Resize recovery and optional XFixes cursor
composition need explicit support, including cursor-only changes that may
not damage the root framebuffer.

At 1080p one 32-bit frame is 8,294,400 bytes; moving that once in 10 ms requires
about 0.83 GB/s. At 4K it is 33,177,600 bytes and 3.32 GB/s. Shared memory removes
socket payload transport but not memory bandwidth, format conversion, process
wakeups or application rendering. Measure all of them before quoting a limit.

**Freshness and acknowledgment are separate contracts**

| Operation | Meaning |
|---|---|
| `act(...)` | Return after the declared X-server input barrier, with an action ID. |
| `screenshot(latest)` | Return the latest completed frame immediately, with capture metadata. |
| `screenshot(after=action_id)` | Perform or select a capture that started after that action's server barrier. The application's pixels may still be unchanged. |
| Optional wait for damage | Wait until a deadline for a later display change. Unrelated animation can satisfy it; it does not prove the action's effect. |

These are proposed API semantics, not currently implemented endpoints.
A single request can combine action, barrier and fresh capture to avoid another
host round trip. Issue the capture after the input barrier explicitly; putting
the two requests on unrelated X connections without coordination is insufficient.
Record capture start as well as completion: a frame published after an action
can have been captured before it. Use one helper clock for ordering, and host
request/return timestamps for host latency; do not subtract uncalibrated clocks
across the sandbox boundary or snapshot generations.

An XDamage-driven cache avoids continuous capture on a static desktop. On-demand
capture must also be available, so the caller never waits forever for damage
on an unchanged screen. Limit background capture work under animation without
imposing an artificial 10 Hz or 60 Hz wait on an explicit capture request.

At 60 frames/s the application frame interval is 16.7 ms; at 24 it is 41.7 ms.
Input handling, rendering and scheduling can add more. Returning existing
pixels in approximately 10 ms is a different requirement from making every
application paint its response within 10 ms. The user's requested immediate
observation semantics allow this distinction without imposing a settle sleep.

**Lifecycle and resource integration must be retained**

The frame ring and control connection are replaceable observation machinery;
applications and their state remain inside the environment. A paused guest
can expose its last frame, clearly marked paused, while rejecting new actions.
Restore must invalidate old action/frame IDs and rebuild or rebind the transport.

The engine has restore mappings for custom descriptors in
`runsc/boot/restore.go`, and host inodes remap through
`pkg/sentry/fsimpl/host/save_restore.go`. However, the inspected standalone
`runsc restore` CLI does not expose `run --pass-fd`'s flag, and our launcher does
not wire arbitrary fast-I/O descriptors into restore. External memfd contents
are not automatically saved as guest RAM. Production integration must either
plumb restore descriptors and reconstruct ring state, or quiesce and remove
the helper's external resources before checkpoint and recreate them afterward.
If choosing teardown, Xvnc must have detached the SHM resources before saving.
Neither approach should discard desktop/application state or weaken existing
CPU checkpoint support. GPU live-graphics snapshot limitations remain as
documented in [snapshot results](gpu-filesystem-snapshots.md).

Our CPU broker can pause guest execution under contention or quota enforcement.
No capture method can acknowledge new X-server work while that server is
unscheduled. Cached reads may stay quick but age increases. Report broker delay
and frame age, and test under contention; a fast idle median is not a scheduler
latency guarantee. Bound and account for host-side frame buffers as well.

**Recommended experiments, in order**

1. In a disposable CPU desktop, verify XTest, MIT-SHM, XDamage, XFixes and
   descriptor-backed SHM capture. First prove pixels change in the host-visible
   buffer; this is the central compatibility test.
2. Compare existing native helper capture, direct SHM-to-memfd capture, and
   control transport costs. Return owned RGB images at 1080p. Reuse the input
   conformance cases before enabling zero action gaps.
3. Measure request/ack, capture request/reply, publication, host copy/conversion
   and complete action-plus-capture latency separately. Report p50/p95/p99,
   failures, retries, dropped frames, CPU cost and frame age. Encode PNG only in
   a separately measured path.
4. Use a visible frame counter or nonce to distinguish changed frames from
   cheap cache hits. Separately measure an instrumented application's response
   to input. Test static screens, full-screen changes and rapid gestures; check
   tearing, resize, cursor-only movement, modifiers, Unicode and grabs.
5. Repeat in a disposable GPU desktop with the existing Firefox/Earth/Resolve
   rendering route, and under CPU/GPU contention. Keep rendering time separate
   from observation delivery. Repeat at higher resolution before generalizing.
6. Validate pause/resume, CPU live save/load, GPU filesystem save/load, helper
   reconnect and stale generation rejection before calling the feature complete.

The decision is to prototype this existing X11/native approach in the standalone
lab. No new VM engine, host privileges, application replacement or GPU capture
framework is justified by the evidence so far. Whether the resulting gVisor
path meets 10 ms at p95 is the experiment's outcome, not an assumed result.

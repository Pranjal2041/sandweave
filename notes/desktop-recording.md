# Optional desktop recording

Implemented for 0.2.14. `Sandbox(recording=True)` and `Pool(recording=True)`
enable desktop recording; the default remains off. `Recording(fps=15,
cursor=True)` configures capture cadence and pointer inclusion. The public API
and recovery examples are in [the desktop guide](../docs/desktop.md).

## Ownership and storage

Each recording runs in a worker-owned subprocess with its own display connection
and FFmpeg encoder. Client actions do not drive capture. Credentials pass over
stdin, and recording data stays outside the guest and its snapshot/cache files.
Lifecycle operations stop capture and finish encoding before detaching controls
or deleting the guest. Pause and snapshot split the recording into segments.
Pool builders and warm members do not record; checkout starts capture.

Finalized files remain on the original worker until explicitly deleted.
Downloads finalize first, verify SHA256, and refuse to overwrite client files.
Recording RPCs work through existing direct and relay connections, including
after sandbox termination and pool baseline reclamation. They retain the same
per-sandbox authorization as commands and files.

Fragmented MP4 preserves completed fragments after a recorder crash. An inherited
file lock prevents exporting or deleting files while an orphaned encoder is
still writing. Process signaling uses the runtime's PID-safe helper, including
on Python builds without standard-library pidfd wrappers. A machine or storage
failure still requires that the original recording storage be recoverable.

## Capture and timing

Cursor-inclusive capture uses a passive, shared RFB 3.8 connection to Xvnc. It
does not send input or clipboard data. ZRLE compression limits traffic through
the guest network relay. Cursor-free capture uses an independent FastIO client
and the existing shared-memory screenshot service.

Advertising RFB Cursor support alone does not suppress the pointer in TigerVNC:
it can render the cursor when another client controls its position. The actual
server behavior is in `VNCSConnectionST::needRenderedCursor` in
[TigerVNC 1.12](https://github.com/TigerVNC/tigervnc/blob/v1.12.0/common/rfb/VNCSConnectionST.cxx).
Password-file DES bytes also require conversion from TigerVNC d3des bit order.
Both behaviors were checked with the real desktop and its installed password.

Each timeline entry identifies a capture request/response interval on the worker
monotonic clock and the corresponding video frame. These are not application
render timestamps. Metadata separates missed capture slots, queue drops,
captured frames, repeated frames and encoded frames. Requested FPS is a target,
not a guarantee. The bounded encoder queue prevents unbounded memory growth.

In the disposable 1920x1080 desktop test on 2026-09-12, cursor-inclusive capture
at a requested 10 FPS measured approximately 4.6 FPS. An uncompressed RFB
experiment fell below 1 FPS through this guest network path, so it was rejected.
These measurements describe capture cadence on that worker, not application FPS
or a throughput guarantee for other machines.

## Acceptance

The integration tests use isolated worker roots and preserve existing desktops:

- `test_desktop_recording_live.py` checks an independently moving X11 window,
  decoded animation positions, actual cursor inclusion, client disconnection,
  pause/resume, memory snapshot/restore, manual stop, local pool checkout and
  downloads after termination. It also exercises CLI download and cursor-free
  video.
- `test_weave_recording_live.py` checks a pool using outbound worker relays,
  kills only its own recorder, closes the pool with baseline reclamation, and
  decodes the recovered partial video through the public download API.
- `test_desktop_recording.py` checks opt-in validation, scoped authorization,
  bounded export paths and chunks, missed capture accounting, and interrupted
  capture with a real encoder.

The final videos are decoded in full for frame-count checks. Sample decoded
frames and cursor crops are inspected visually. The desktop documentation is
rendered at desktop and mobile widths, including the example copy button.
Release validation also builds and installs the wheel outside the checkout;
live acceptance is repeated against that installed artifact before publishing.

# Local timestamped PNG capture, 2026-09-20

This branch adds `desktop.capture_png(cursor=True)` without changing the
existing screenshot return type or input API. It requires rebuilt desktop
assets as well as the candidate SDK/worker. It is local-only, not published.

The native Xvnc shared-memory helper records guest monotonic and Unix capture
intervals, optionally composites the XFixes cursor, and retains frame generation
and input-sequence checks. The worker returns full-resolution lossless PNG bytes
and matching metadata in one response. Encoding uses PNG filter 0, zlib level 1,
and the default compression strategy. A previously tried RLE-only strategy was
slower and produced much larger files for the benchmark scene.

The capture lock pairs an owned image with its metadata; PNG encoding runs after
releasing it. Input-only calls do not acquire this lock. A new capture opcode is
advertised in the helper handshake, so an old helper rejects the operation before
an unsupported request can terminate its channel.

## Acceptance

Final isolated candidate on orchard-flame-2:
`/tmp/sw-fast-capture-final-20260920`.

- SDK unit suite: 453 passed, 139 skipped.
- `scripts/test-fast-io.py`: 15 passed.
- Actual GNOME captures included the cursor at the center and display edges.
  Capture worked after sandbox pause/resume with a new helper generation.
- A real Rotating Keyboard input was visible in a fresh subsequent capture with
  its input sequence. A separate benchmark adapter retained the public browser
  clock, paused behavior, six-frame windows, and lossless 1920x1080 observations.
- Final keyboard, post-input, and cursor-edge images were opened and inspected.

Thirty interleaved before/after samples in the same benchmark sandbox measured
**594.82 ms → 54.54 ms** for the complete benchmark capture return (PNG and
manifest saved, browser clock retrieved). Native p95 was **65.15 ms**. The native
PNG API itself averaged **46.30 ms**: capture **2.46 ms**, PNG encode **36.58 ms**,
remaining copy/dispatch/serialization/transport **7.25 ms**. The latter is not a
measurement of network alone. The PNG sizes were 410,486 → 409,277 bytes.

Timing is content-dependent. The separate GNOME wallpaper test measured
**128.75 ms** for native PNG plus client write, compared with **734.83 ms** for
the existing raw-image API followed by Pillow's default PNG encoding. This is
not a claim that all possible images take less than 100 ms.

Reproduction scripts:

- `scripts/profile-native-png.py`: owns and closes its sandbox and idle worker.
- `scripts/profile-png-encoding.py`: compares lossless encoding strategies on
  a saved frame, without touching a sandbox.
- Benchmark-side `tools/profile_native_capture_integration.py` in
  `/data/user_data/pranjala/weird-cua-bench`: sequential native/FFmpeg comparison,
  browser-clock and paused-window checks.

Durable measurements and images:
`/data/user_data/pranjala/weird-cua-bench/outputs/native_capture_20260920/`.
The full benchmark report is
`/data/user_data/pranjala/weird-cua-bench/docs/native-screenshot-profile-20260920.md`.
Its broader test suite is not green; the report records failures and the
interrupted full-suite attempt separately from successful focused checks.

The official SDK, shared evaluation worker assets, active evaluation source, and
Gym-Anything repository were not changed. No existing sandbox was borrowed for
testing. This branch is based on the earlier local ACK fixes in `17d8b5f`; the
timed before/after screenshot paths both ran on the same candidate worker.

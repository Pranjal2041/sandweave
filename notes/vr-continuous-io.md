# Continuous VR observations, inputs and recording

The sandbox can deliver a continuous stream of composed VR eye images directly
to a host Python client while receiving controller/headset state on a persistent
connection. Images are available independently of the optional Xvnc desktop
mirror. This is an experimental Monado integration in the existing no-sudo,
no-KVM gVisor sandbox; it does not change the gVisor engine.

## Data path and ownership

```
Open Saber OpenXR stereo images → Monado GPU composition
                                  ├─ Vulkan readback → sealed shared-memory ring
                                  │                     ├─ model/client: latest owned image
                                  │                     └─ bounded background recorder
                                  └─ optional one-shot eye capture
Open Saber desktop mirror → optional VirtualGL readback → Xvnc

host client → persistent stdio bridge → Monado remote TCP → OpenXR device state
            ← acknowledged matching state after Monado installs it
```

The game continues rendering both 1344×1512 eye views. The observation stream in
these tests is the **composed left eye resized to 960×1080**, four bytes per pixel
(RGBX8). It is an image stream, not a video-decoding interface. The video is a
secondary visualization of the recorded images. Physical headset delivery and
hardware tracking are not part of these tests.

`VRStream.latest(after=sequence)` reads the newest complete frame. Each returned
`Frame` owns its pixel bytes: a subsequent producer update cannot alter a model's
observation. The ring has eight slots by default (about 31.6 MiB). A seqlock rejects
a frame overwritten during the copy. A slow consumer skips old frames instead of
blocking the compositor or accumulating an unbounded observation backlog. The
measurement script reports skipped ring sequences separately from recording
queue overflow.

The ring is a host `memfd` donated explicitly through Apptainer and gVisor's
`exec --pass-fd`. Its length is sealed against shrink/grow, including guest-root
attempts, and its dimensions are validated on both sides. The host reader uses
its own fixed dimensions and bounds rather than trusting mutable guest headers.
A live guest-root `ftruncate(3, 0)` probe returned EPERM and preserved the mapping.
The FD is scoped to the stream owner; no shared directory or network port is
exposed to other sandboxes by this interface.

Readback currently uses Monado's synchronous Vulkan submit/wait helper. It does
not implement an asynchronous GPU transfer pipeline or GPU tensors shared with
the model. We measure that cost, rather than treating no-desktop-mirror game FPS
as observation throughput.

## Input and acknowledgement

The persistent connection sends headset and controller state: positions,
normalized quaternions, velocities, analog controls and button/touch values.
Positions are absolute tracking-space meters; quaternions use x/y/z/w order.
Omitted fields persist. Each update is fully validated before transmission.
Unknown fields, invalid dimensions, nonfinite values, zero quaternions and
out-of-range analog controls are rejected without applying a partial update.

The experimental `mndack1` packet header requests an echo after Monado installs
the complete state. The ordinary `mndrmt3` protocol and one-shot CLI remain
available. Driver readers take a mutex-protected copy so streaming updates do
not tear a controller/head pose during ordinary runtime queries. The remote
debug GUI is not used in this experiment.

The host ACK means **Monado received the matching state**. It does not assert
that the application has polled the state, updated physics, rendered the result,
or delivered it to a physical headset. A failed/disconnected input channel is
poisoned; the client does not automatically replay an action with an unknown
outcome. Normal connection closure releases held buttons and analog axes.

The acceptance test generates a timed trajectory for both controllers and small
head movements at a requested 120 updates/s. It is a scripted virtual-device
control, not a learned policy, successful game-playing agent or physical tracker.
The input log records every submitted state and its acknowledgement timestamps.

## Background storage and video

`FrameRecorder` has a bounded queue of 32 owned frames (about 126.6 MiB at the
tested resolution) and two encoding/writing workers. Each image is saved as an
independent, lossless `.rgba.zst` file. `frames.jsonl` identifies its filename,
size, pixel format, sequence and timestamps. Storage errors propagate when the
recorder closes; queue overflow is counted explicitly rather than hidden.
Model observations continue if the storage consumer falls behind.

PNG encoding was a concrete recording bottleneck: a sampled gameplay image took
45.6 ms to encode with Pillow's level-1 PNG, compared with 8.4 ms for level-1
Zstandard on its RGBX bytes, at similar compressed size. Two PNG workers dropped
152/1058 frames; four still dropped 60/1416 in another run. The first Zstandard
run saved all 1021 delivered observations, with zero recording or ring skips.
Its background encoding median was 6.2 ms, and file-write median 1.6 ms.

`RecordedFrames` reconstructs the exact original pixel bytes and metadata.
The video exporter runs **after** live recording closes. It decodes the images,
creates temporary PNGs, and uses their capture timestamps for variable-rate H.264
output; the image-demuxer timebase is explicitly 1 ms rather than its default
25 Hz. The temporary PNGs are removed. Video encoding is lossy; source images
remain lossless and independently accessible.

## Observed delivery with continuous input

| Configuration | Images received/s | Game submissions/s | Input updates/s | Saved / delivered |
|---|---:|---:|---:|---:|
| Memory only, capture cap 90 | 74.1 | 104.7 | 118.3 | Not recorded |
| Zstandard recording, capture cap 90 | 51.1 | 71.2 | 119.1 | 1021 / 1021 |
| Zstandard recording, capture cap 120 | 64.0 | 66.1 | 119.8 | 1280 / 1280 |

These runs had **zero host ring skips**. Different shared-GPU activity and
short windows prevent treating differences between rows as isolated recorder
or capture-rate costs. The first row used a regular-file ring; the subsequent
recording rows used the final size-sealed memfd ring.

In the final 120-cap recording, latency in milliseconds was:

| Stage | Median | p95 | p99 |
|---|---:|---:|---:|
| Readback work and GPU completion wait | 2.06 | 12.09 | 270.82 |
| Copy into shared ring | 0.32 | 0.76 | 0.97 |
| Publication to owned host pixels, including polling/copy | 0.72 | 2.35 | 3.38 |
| Readback beginning to owned host pixels | 3.64 | 13.12 | 271.65 |
| Input to Monado acknowledgement, round trip | 0.62 | 1.85 | 4.01 |

The worst readback-to-client sample was 421 ms. This is **not** a guarantee of
10 ms delivery or a steady 60/90 Hz control loop. Typical operation is fast;
long-tail readback stalls remain material. Summing individual stage percentiles
would not produce the total percentile because they can represent different frames.

Full timing distributions and the earlier PNG failures are retained in
[the measurement summary](vr-continuous-io-measurements.json). The final viewing
artifact is `runs/vr/continuous-120-zstd/frames/gameplay.mp4`, with 1280 encoded
frames over 20.000 seconds. Its lossless source images and timeline are adjacent;
`runs/vr/continuous-120-zstd/inputs.jsonl` contains 2396 acknowledged updates.
Decoded video images at 5 and 15 seconds were inspected: controllers move, blocks
advance, and the score changes from 0 to 100. Audio remains unavailable.

## Python and CLI use

Host dependencies used here: Pillow 12.0.0, zstandard 0.24.0, protobuf, and ffmpeg
for optional video export. The guest bridge uses only Python's standard library. Direct `runsc exec --user`
retained guest capabilities and failed Vulkan initialization in the first launch;
launching through `runuser` matched the working ordinary-user context. The
specific capability or context difference responsible was not independently isolated.
`prepare-vr-lab.py` stages the new bridge/codec with the pinned Monado source and
cumulative patch. Build dependencies are installed inside the sandbox.

Stop the current experimental game/runtime before opening the owned stream:

```bash
python scripts/vr-lab.py vr-monado-01 stop
PYTHONPATH=scripts python - <<'PY'
from vr_stream import VRStream, FrameRecorder

recorder = FrameRecorder('runs/vr/example-images')
try:
    with VRStream('vr-monado-01', fps=90, mirror='none') as vr:
        last = 0
        for _ in range(300):
            frame = vr.latest(after=last)
            last = frame.sequence
            # frame.rgba is an owned RGBX byte buffer; frame.image() returns PIL RGB.
            recorder.submit(frame)
            ack = vr.input({'right': {'position': [0.2, 1.3, -0.5],
                                      'orientation': [0, 0, 0, 1]}})
finally:
    recorder.close()
print(recorder.saved, recorder.dropped)
PY
```

For the measured gameplay trajectory, independent input thread, timing report,
recorded images and viewing video:

```bash
python scripts/vr-stream.py vr-monado-01 --fps 90 --input-hz 120 \
  --seconds 20 --record --video --output runs/vr/my-recording
```

Read a saved image without video decoding:

```python
from vr_stream import RecordedFrames

frames = RecordedFrames('runs/vr/my-recording/frames')
frame = frames[100]
pixels = frame.rgba
rgb_image = frame.image()
print(frame.sequence, frame.capture_begin_ns, frame.host_observed_ns)
```

The stream context owns the experiment's Monado/game processes, not the whole
sandbox or Xvnc. It holds the shared environment lifecycle lock and an exclusive
VR-owner lock. Close the stream before pause/save/stop; this version does not
transparently detach and reconnect through those operations. Existing graphics
live-snapshot limitations still apply. Other desktops remain running.

## Timing semantics and limits

The report retains these timestamps for each image:

- `capture_begin_ns`: compositor starts the readback work, after scheduling composition.
- `gpu_ready_ns`: synchronous readback completed; CPU can read the pixels.
- `published_ns`: copying the image into the shared ring finished.
- `host_observed_ns`: the host client finished its own stable pixel copy.
- `predicted_display_ns`: runtime's predicted display time; **not** a physical display timestamp.

The first four distinguish readback, publication, polling and client copy.
Guest and host monotonic clocks have different epochs. Twenty bridge pings
estimate their offset from the lowest-RTT midpoint; the report includes half
that RTT as calibration uncertainty. Within-guest durations need no conversion.

**Capture-begin-to-client latency is not full action-to-visible-effect latency.**
It excludes the earlier game input/physics/render cycle and model inference,
preprocessing, remote networking and physical headset transport. New compositor
sequences can reuse application frames, so observation FPS also does not prove
an equal number of unique game-state updates. Monado metrics separately report
application submissions and reused compositor frames.

All runs used the same shared L40S, four advertised guest CPUs and weighted
sharing over the Slurm CPU pool. They are short exploratory windows, not a
controlled or exclusive-GPU benchmark. During investigation the selected GPU
also held a roughly 40 GiB vLLM worker and other CUDA clients. One-second GPU
process samples observed the vLLM worker reaching 96% SM utilization while VR
was present; the two existing
Resolve desktops were preserved. Their presence is not proof that they caused
all observed stalls. Some readback sections took hundreds of milliseconds even
with recording disabled; the host ring-copy and delivery sections were much
shorter. The root cause of that tail has not been conclusively isolated.

The CLI's `--fps` is a maximum capture cadence, not a throughput guarantee.
A phase-preserving deadline replaced the initial last-frame-plus-period limiter,
which inadvertently rounded a 90 Hz request toward 60 Hz on a 120 Hz compositor.
Neither CPU affinity, GPU exclusivity nor the sandbox resource policies were
changed for these tests.

## Verification

- Real Open Saber OpenXR frames reached the host memory API and changed during
  continuous virtual-controller input; gameplay captures and video frames were inspected.
- Background compressed-image roundtrip preserves every byte and timestamp.
- Torn/incomplete/overwritten frames are rejected; slow readers skip safely.
- Input validation is atomic; failed channels do not replay actions.
- Buffer-size sealing works on the host and from guest root.
- Recorder overflow is observable, and storage failures reach the caller.
- Busy-owner rejection preserves the running VR session; the original one-shot
  input and eye capture still worked after restoring the live Xvnc mirror.
- The cumulative patch applies to the pinned upstream source; guest Monado built.
- Relevant lab tests: `test-vr-stream.py`, `test-fast-io.py`, `test-environment.py`.

Generated recordings, binaries, GPU activity logs and raw measurements remain
under ignored `runs/` and `tools/`. The code, source patch and summarized
measurement evidence are committed in this standalone lab.

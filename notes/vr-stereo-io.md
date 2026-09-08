# Paired stereo VR observations

Live observations and recordings now contain **both eyes from the same Monado
compositor frame**. This replaces the earlier left-eye-only observation path.
The game still renders at 1344×1512 per eye; the default capture is 960×1080
per eye. Each observation contains a 1920×1080 left/right side-by-side RGBX8
buffer, one sequence, one compositor frame ID and shared timestamps.

## Implementation and model API

The compositor selects both scratch-image views from the same frame state.
Two compute blits resize them into disjoint halves of one GPU image, followed
by one combined GPU-to-CPU transfer and completion wait. Only then does it
publish the complete pair into the size-sealed host memfd ring. There is no
independent left/right polling or second-eye request. One-shot captures also
contain both eyes.

Ring version 2 explicitly marks each slot as stereo. The host rejects incomplete
transactions and slots without that marker. Its owned copy remains stable if
the compositor overwrites the ring. The default eight slots occupy 63.3 MiB.

```python
frame = vr.latest(after=last_sequence)
left, right = frame.left, frame.right  # each: (1080, 960, 3), uint8 RGB
sequence = frame.sequence
compositor_frame = frame.compositor_frame
left_image = frame.image('left')
right_image = frame.image('right')
side_by_side = frame.image()
```

The arrays are read-only, strided NumPy views of the same owned buffer; accessing
the second eye does not copy its pixels again. `VRStream` and CLI dimensions
are **per eye**. `Frame.width` is the packed width, `Frame.eye_width` is the
individual width, and every live `Frame.eye_count` is 2.

Background recording saves complete pairs as lossless `.rgba.zst` files with
`eye_count: 2` in `frames.jsonl`. The default is now four recording workers and
a bounded queue of 32 pairs (253.1 MiB, excluding workers/current frames). A
slow recorder may drop complete pairs; the observation loop does not block on
storage. Historical recordings remain readable as mono and reject right-eye
access. The video exporter consumes the recorded pairs after recording closes.

## Live acceptance

Open Saber ran in `vr-monado-01` on `babel-u5-28`, Slurm allocation 10333558,
using the shared L40S, gVisor and unprivileged Apptainer, without host sudo or
KVM. Each run requested a 120 Hz compositor, up to 120 stereo observations/s
and 120 virtual-device updates/s, with 20 seconds of warmup and a 20-second
measurement window. The desktop mirror was disabled during measurement;
observations came directly from Monado. Both controllers and the headset
received the scripted trajectory throughout recording.

| Run | Stereo pairs/s | Input updates/s | Pairs saved / delivered | Ring skips | Recording drops |
|---|---:|---:|---:|---:|---:|
| Initial, two recorder workers | 89.38 | 120.01 | 1515 / 1797 | 1 | 282 |
| Final, four recorder workers | **71.79** | **118.66** | **1438 / 1438** | **0** | **0** |

The first run exposed insufficient recording capacity. The rerun with four
workers saved every delivered pair. These short runs used a shared GPU under
uncontrolled load; their rates do not isolate the cost of the worker change,
and comparisons with historical mono runs do not establish a stereo speedup.

Final-run latency, in milliseconds:

| Stage | Median | p95 | p99 | Maximum |
|---|---:|---:|---:|---:|
| Readback work and GPU completion wait | 2.17 | 7.38 | 256.75 | 306.41 |
| Copy into shared ring | 0.58 | 1.28 | 1.81 | 5.01 |
| Publication to owned host pixels | 1.33 | 5.04 | 7.34 | 14.35 |
| Readback beginning to owned host pixels | **5.05** | **10.40** | 258.17 | 308.12 |
| Input to Monado acknowledgement | 0.61 | 2.17 | 5.81 | 195.41 |

Long-tail stalls remain. These results do not guarantee steady 72/90 Hz or a
10 ms deadline. Readback-to-client timing excludes the earlier game update and
render cycle. Input acknowledgement means Monado installed the device state,
not that the game rendered its effect. Monado reported 74.85 application
submissions/s and a 37.0% compositor-frame reuse fraction: a new stereo pair
does not necessarily represent a new application frame. No physical headset
scanout or transport was tested.

The [machine-readable report](vr-stereo-io-measurements.json) preserves both
run summaries, pair validation and video metadata. The existing
[continuous I/O notes](vr-continuous-io.md) describe ownership, input semantics,
clock calibration and lifecycle constraints.

## Artifacts and verification

Final artifacts under `runs/vr/stereo-120-complete/`:

- `frames/frames.jsonl` and adjacent `.rgba.zst` files: all 1438 lossless pairs.
- `inputs.jsonl`: 2376 acknowledged headset/controller updates.
- `frames/gameplay.mp4`: 1920×1080 side-by-side video, 1438 frames, 19.785 seconds.
- `left-eye.png`, `right-eye.png`, `gameplay-end.png`: separate eyes and the pair.
- `eye-validation.json`: stereo metadata for all pairs and 16 sampled image checks.

The sampled left/right arrays had the expected shapes, read-only storage and
distinct pixels. Decoded video images at 5 and 15 seconds were visually inspected:
both eyes show consistent scenes with different perspectives, moving controllers
and advancing gameplay. The video preserves capture timestamps and is lossy;
the original image files retain exact pixels. Audio is not included.

All 34 relevant lab tests passed: 12 VR-stream tests, 13 fast-I/O tests and 9
environment tests. Stereo coverage includes distinct eye views, atomic pair
publication, rejection of mono slot markers, lossless round trips and honest
historical mono handling. The cumulative Monado patch applies cleanly to pinned
upstream `f8dfadfeaeb46df3eec17bd76b7abdf42a79108c` and builds successfully.

The live PBO desktop mirror was restored after measurement, both game/runtime
services are active, and a fresh one-shot capture was visually verified as
stereo. VR VNC remains on port 41647. Both existing Resolve desktops remain
running. The gVisor source checkout was unchanged.

To repeat in the prepared VR sandbox, explicitly stop its current experimental
game/runtime first, then choose a fresh output directory:

```bash
python scripts/vr-lab.py vr-monado-01 stop
python scripts/vr-stream.py vr-monado-01 --fps 120 --input-hz 120 --hz 120 \
  --width 960 --height 1080 --warmup 20 --seconds 20 --mirror none \
  --record --video --output runs/vr/my-stereo-recording
python scripts/vr-lab.py vr-monado-01 start --hz 120 --mirror pbo
```

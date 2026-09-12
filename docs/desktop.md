# Desktop agents

The GNOME template provides a desktop, screenshots, and mouse and keyboard input:

```python
from sandweave import Sandbox

env = Sandbox(template="gnome")
image = env.desktop.screenshot()
image.save("desktop.png")
```

Dependencies are prepared on first use. GNOME starts at 1920×1080 using Xvnc.
Add `target="lab"` or a printed cluster address to run on a cluster worker.

For a GPU game example, see [Stunt Rally 3](https://github.com/Pranjal2041/sandweave/tree/main/examples/stuntrally3).
It starts the standalone Linux game at 1920×1080, accepts keyboard controls,
and can record the desktop.

## Install an application

```python
env.setup("./install-chrome-and-myapp.sh")
```

Write the setup script to install your applications. Choosing the GNOME template
does not install Chrome or application-specific software automatically.

## Send input

```python
env.desktop.mouse.click(400, 300)
env.desktop.keyboard.press("ctrl+l")
env.desktop.keyboard.type("hello")
image = env.desktop.screenshot()
```

Mouse coordinates refer to the desktop image. Keyboard input goes to the
focused window; these calls do not choose an application for you.

## Run an agent loop

```python
from sandweave import Sandbox

def desktop_episode(policy):
    with Sandbox(template="gnome") as env:
        image = env.desktop.screenshot()
        for _ in range(100):
            action = policy(image)
            if action is None:
                break
            observation = env.desktop.step(action)
            image = observation.image
```

Provide a policy that returns desktop actions. For example, a mouse click uses
`{"mouse": {"left_click": [400, 300]}}`. `step` captures an observation after the
input server acknowledges the action. An application may still be processing
the input or repainting; add task-specific readiness checks when necessary.

## Record a desktop

Recording is off by default. Enable it for an individual sandbox:

```python
from sandweave import Sandbox

with Sandbox(template="gnome", recording=True) as env:
    env.desktop.keyboard.press("super")

env.recording.download("./episode")
```

Capture starts when the desktop is ready and continues between actions, including
while your client waits or disconnects. Termination finalizes the recording
before cleaning up the guest. The recording stays on the worker and can be
downloaded after the sandbox closes.

Choose capture cadence and cursor inclusion with `Recording`:

```python
from sandweave import Recording

with Sandbox(template="gnome", recording=Recording(fps=10, cursor=False)) as env:
    env.desktop.mouse.click(400, 300)

env.recording.download("./episode-without-cursor")
```

The default is 15 captures per second with the actual desktop cursor included.
`fps` accepts integers from 1 to 60. It requests a capture cadence; it does not
set the application's frame rate. Capture and encoding consume worker CPU, memory
and storage only when recording is enabled. A bounded frame queue drops frames
if encoding falls behind.

### Files and timing

The download contains `recording.json` and a directory for each video segment.
Each segment includes:

- `video.mp4`: H.264 desktop video.
- `timeline.jsonl`: capture request/response timestamps and their video positions.
- `segment.json`: capture cadence, encoded frame counts, missed capture slots,
  frames dropped at the encoder queue, and repeated video frames.
- `encoder.log`: encoder diagnostics, if any.

Timestamps use the worker's monotonic clock. `started_at` anchors each segment
to Unix time. They measure display capture, not application render timestamps.
When a capture is missed, the video holds the previous frame to preserve elapsed
time; repeated frames are reported separately from captured frames. Videos use
lossy encoding. A display resize fits the whole desktop into the segment's initial
dimensions, with padding where needed; the timeline records the source dimensions.

Pause and snapshot operations finalize the current segment. Resume starts another
segment; the manifest preserves the gap. A restored sandbox records only if you
explicitly pass `recording=True` again.

### Stop, recover, or delete a recording

```python
print(env.recording.info)
env.recording.stop()
env.recording.download("./saved-episode")
env.recording.delete()
```

`stop()` leaves the desktop running. `download()` also stops recording, verifies
file checksums, and writes to an empty client directory. These methods support
`.aio`. `env.info["recording"]` includes status for a sandbox created with recording
enabled.

To retrieve a recording from another process, use the sandbox's ID and its
original target:

```python
with Sandbox.connect(sandbox_id, target="lab") as env:
    env.recording.download("./recovered-episode")
```

The recorder runs independently of the client and worker server process. If the
display or recorder fails, its metadata reports a partial recording. Completed
MP4 fragments remain retrievable; a crash can lose the final incomplete fragment.
The first fragment must have been written for a video to be recoverable.

Recordings live under the worker workspace's `recordings/<sandbox-id>` directory,
outside the guest, its snapshots and pool caches. Sandbox termination and
`retain_baseline=False` preserve them. Download does not delete the worker copy;
use `delete()` to reclaim it. Availability after a machine or storage failure
depends on the worker's storage, and downloads require a reachable worker.

### Record pool episodes

```python
from sandweave import Pool

with Pool(target="lab", template="gnome", size=4, warm=2, recording=True) as pool:
    with pool.acquire() as env:
        env.desktop.keyboard.press("super")
    env.recording.download("./pool-episode")
```

Only checked-out environments record. Baseline builders and the warm reserve do
not record. Releasing a lease finalizes its recording before guest cleanup.
Each sandbox has its own retained recording, including in a pool that removes
its baseline files on closure.

Desktop recording requires the gVisor Xvnc desktop and Sandweave 0.2.14 or newer
on clients, controllers and workers. Its dependencies install with the desktop;
no guest internet access or package installation is needed to record an existing
desktop. For paired VR eye videos, use [VR recording](vr.md).

## Find the VNC address and password

```python
info = env.info
print(info["vnc"]["url"])
print(info["vnc"]["password"])
```

The VNC address points to the worker's loopback port. Remote VNC access uses your
own network or tunnel arrangement. A cluster's command transport does not
automatically forward that separate TCP connection.

## Measure startup

```python
print(env.timings)
```

`ready_seconds` measures startup on the worker. Other entries break down runtime
launch, setup, and desktop readiness. First-use installation and worker preparation
happen before that timer starts.

## Reuse the installed desktop

```python
baseline = env.cache("my-desktop")
env.terminate()

with Sandbox(cache=baseline) as restored:
    restored.desktop.screenshot().save("restored.png")
```

This saves installed files and the template. Restoring a filesystem cache starts
a fresh desktop session. Use `detached=True` when you want the current session
to survive your Python process exiting; see [cleanup](lifecycle.md).

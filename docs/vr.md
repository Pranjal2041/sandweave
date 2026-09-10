# VR games

VR templates provide paired left and right eye images, virtual controllers,
and stereo recording. A compatible NVIDIA GPU must already be available to the
worker.

## Prepare a game

Open Saber downloads automatically during preparation:

```bash
sandweave setup --template vr/opensaber
```

For GunSpinning VR:

```bash
sandweave setup --template vr/gunspinning
```

Its first installation asks for the Linux ZIP from the
[official game page](https://demonixis.itch.io/gunspinning-vr). For unattended
setup, provide the archive with `--game-archive /path/to/game.zip`.

| Template | Presentation |
| --- | --- |
| `vr/opensaber` | VR with paired eye observations. |
| `vr/gunspinning` | VR with motion controller input and paired eye observations. |
| `games/gunspinning-gamepad` | Flat gamepad mode with desktop output; no stereo observation. |

## Observe both eyes

```python
from sandweave import Sandbox

with Sandbox(template="vr/opensaber", gpu=True) as env:
    observation = env.vr.observe()
    left, right = observation.left, observation.right
```

The two views come from the same compositor frame. Use both views when evaluating
or recording a VR interaction.

## Send a controller action

```python
action = {
    "head": {
        "position": [0.0, 1.6, 0.0],
        "orientation": [0.0, 0.0, 0.0, 1.0],
    },
    "right": {"trigger_value": 1.0, "trigger_click": True},
}
```

Pass an action to `env.vr.step(action)` while the sandbox is running. Omitted
fields keep their previous values; send a later action to release a held trigger.
The returned capture follows the runtime's input acknowledgement, which does
not guarantee a fixed simulation step or that the game consumed the action.

## Record an agent episode

```python
from sandweave import Sandbox

def play_episode(policy):
    with Sandbox(template="vr/gunspinning", gpu=True) as env:
        observation = env.vr.observe()
        with env.vr.record("./episode", fps=30):
            for _ in range(300):
                action = policy(observation.left, observation.right)
                if action is None:
                    break
                observation = env.vr.step(action)
```

Provide your own policy. Recording saves lossless paired frames, separate left
and right MP4 previews, a synchronized side-by-side preview, and timing and drop
metadata. The previews are lossy. `fps=30` is the requested capture cadence,
not the application's frame rate.

Video export needs FFmpeg with `libx264` on the client and at least two recorded
frames. `sandweave doctor` can install and register FFmpeg for export.

## Reuse a prepared game offline

```python
from sandweave import Sandbox

with Sandbox(template="vr/gunspinning", gpu=True) as env:
    ready = env.cache("gunspinning-ready")

with Sandbox(cache=ready, gpu=True, network="offline") as env:
    observation = env.vr.observe()
```

Filesystem restores start fresh game processes. They do not restore a live
graphics context or the exact moment of gameplay.

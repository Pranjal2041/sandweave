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

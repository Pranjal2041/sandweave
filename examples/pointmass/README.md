# A separately installed control provider

Install this example into both the SDK client and worker environments:

```sh
python -m pip install ./examples/pointmass
```

```python
from sandweave import Sandbox

with Sandbox(template="examples/pointmass/template.toml") as env:
    robot = env.capability("robotics")
    robot.reset(position=0)
    print(robot.step(acceleration=2))
```

The example advances a one-dimensional point mass inside the guest. Its state
survives filesystem and memory capture. It demonstrates installation, discovery,
template attachment and custom action/observation schemas. It does not implement
RoboCasa or realistic robot physics.

A control provider registers a `sandweave.controls.v1` entry point and declares
`api_version = 1`. `descriptor` describes its schema; `bind` creates its client;
`attach` creates its worker attachment. The attachment handles `call` and
`detach`. `Context` supplies guest commands, files, the selected runtime and
the remaining startup deadline. Built-in desktop, gamepad and VR controls use
the same registration contract. Plugins are trusted worker code and must be
installed with matching versions on both sides.

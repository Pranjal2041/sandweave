# Templates

A template supplies installed software, startup services, and controls. Resource
overrides such as `cpu=4` or `memory="8GiB"` belong on `Sandbox(...)`; they do
not require another template.

## Built-in templates

| Name | Includes |
| --- | --- |
| `coding` | Python, a shell, and file access. The default. |
| `gnome` | GNOME desktop, screenshots, mouse and keyboard input. |
| `cuda` | Coding tools and one eligible NVIDIA GPU. |
| `docker` | A Docker daemon inside the sandbox. |
| `vr/opensaber` | Open Saber, virtual controllers, and paired eye images. |
| `vr/gunspinning` | GunSpinning VR with motion controllers and paired eye images. |
| `games/gunspinning-gamepad` | GunSpinning's flat gamepad mode. |

```python
from sandweave import Sandbox

env = Sandbox(template="gnome", cpu=4, memory="8GiB")
```

Templates install on first use. `sandweave setup --template gnome` prepares one
ahead of time.

## Extend a template

Create these two files in your project:

```toml title="my-desktop.toml"
extends = "gnome"

[setup]
script = "install-tools.sh"
```

```bash title="install-tools.sh"
#!/bin/sh
set -eu
apt-get update
apt-get install -y jq
```

Then create the environment:

```python
from sandweave import Sandbox

with Sandbox(template="./my-desktop.toml") as env:
    print(env.run("jq --version").stdout)
    env.desktop.screenshot().save("desktop.png")
```

Setup paths in TOML are relative to the template file. A child inherits its
parent's installation recipe and controls.

## Declare additional setup inputs

```toml
extends = "coding"

[setup]
script = "install-project.sh"
inputs = ["requirements.txt", "app"]
```

Create the named script and input files within its directory. Declared bytes are
sent to the sandbox and included when deciding whether cached preparation matches.
Undeclared local files are not copied automatically.

## Start a service on every cold boot

If your setup script installs `/workspace/app.py`, a template can start it and
wait for readiness:

```toml
[services.my_app]
command = "python /workspace/app.py"
ready = { exec = "curl -fsS http://localhost:8000/health" }
```

The application must provide the health route, and the image must include the
tools used by the readiness command. Filesystem restores start services again;
supported memory restores preserve existing processes.

## Add domain-specific controls

Controls can be supplied by a separately installed Python package through the
`sandweave.controls.v1` entry point. Client and worker must have matching provider
versions. A template declares the provider and its configuration.

The repository's [point-mass example](https://github.com/Pranjal2041/sandweave/tree/main/examples/pointmass)
shows how to add controls without changing `Sandbox`. A robotics template must
supply its actual simulator and action implementation; a generic robotics
adapter is not bundled with Sandweave.

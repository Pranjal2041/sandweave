# Docker images

Use a Docker or OCI registry image as the sandbox's filesystem:

```python
from sandweave import Sandbox

with Sandbox(image="docker://python:3.12-slim") as env:
    print(env.run("python --version").stdout)
    print(env.info["image"])
```

The worker downloads and prepares the image on first use. Later sandboxes reuse
the prepared image with separate writable filesystems. A Docker daemon is not
required. Images run through Sandweave's existing gVisor runtime, without KVM
or host sudo.

## Combine an image with a template

An image supplies files and installed software. A template supplies setup,
services, resources, and controls. A custom template can choose its default image:

```toml title="python-env.toml"
image = "docker://python:3.12-slim"

[setup]
script = "install.sh"
```

```bash title="install.sh"
#!/bin/sh
set -eu
python -m pip install pytest
```

```python
from sandweave import Sandbox

with Sandbox(template="./python-env.toml") as env:
    print(env.run("pytest --version").stdout)

# Override the base and keep the template's setup and services.
with Sandbox(template="./python-env.toml", image="docker://python:3.13-slim") as env:
    print(env.run("python --version").stdout)
```

Setup scripts and services must support the selected image. For example, a
script using `apt-get` needs a base that provides it. Selecting the `gnome`
template with a different image does not install GNOME into that image; its
desktop software and startup programs must already be present.

## Image defaults

Commands inherit the image's `ENV`, `USER`, and `WORKDIR`. Template settings
override image settings; constructor `env=` overrides the template environment.
Per-command `cwd`, `env`, and `user` apply only to that command. An image without
a working directory uses `/`. Existing templates without an image retain their
usual `/workspace` default.

Sandweave starts its command service instead of the image's `ENTRYPOINT` and
`CMD`. Declare applications under [template services](templates.md#start-a-service-on-every-cold-boot)
to start them on each cold boot. The original entrypoint and command are retained
in `env.spec["image"]["settings"]`.

Images do not need Python. Sandweave keeps its own control runtime in a private
directory without replacing the image's Python or libraries. If an image has
no shell, use literal arguments:

```python
result = env.run(argv=["/app/program", "--version"])
```

## Reuse and reproducibility

A tag is resolved when first used in the selected Sandweave storage directory.
Subsequent launches reuse that resolution without contacting the registry.
Pass `refresh=True` to resolve the tag again:

```python
with Sandbox(image="docker://python:3.12-slim", refresh=True) as env:
    print(env.info["image"]["digest"])
```

For an exact base across machines, pass a digest reference such as
`docker://registry.example.org/team/image@sha256:<digest>`.
`env.info["image"]["digest"]` reports the selected Linux image manifest digest.
Caches and snapshots retain that image; restoring them does not resolve its tag
again. `cache_key=` also caches completed template setup.

Image downloads and preparation require worker network access on first use.
`network="offline"` disables the guest's internet access; it does not disable
worker-side downloads. Blobs and prepared images stay under `images` in the
selected Sandweave storage directory. They are not automatically evicted.

## CLI and pools

```bash
sandweave run --image docker://python:3.12-slim -- "python --version"
```

```python
from sandweave import Pool

with Pool(image="docker://python:3.12-slim", size=2) as pool:
    with pool.acquire() as env:
        print(env.run("python --version").stdout)
```

`target=` places image-backed sandboxes on a cluster in the same way as other
sandboxes. Each worker prepares an image when it first needs it. Pool baselines
and snapshot transfers carry the prepared filesystem.

## Supported images

The initial implementation supports public registries over HTTPS, Docker v2
and OCI manifests, and Linux x86-64 images. Gzip, Zstandard, and uncompressed
layers are supported. Other CPU architectures, Windows images, private registry
authentication, and sparse tar entries are not supported.

Image support does not implement the Docker API. Evaluation harnesses that call
Docker directly still need a Sandweave adapter. Applications also remain subject
to [gVisor's Linux compatibility](https://gvisor.dev/docs/user_guide/compatibility/);
an image cannot enable unsupported kernel features or privileged device access.

---
title: Introduction
description: Create Linux sandboxes, run code, control desktops and VR games, and distribute work across your own machines.
---

<div class="sw-eyebrow">Sandweave documentation</div>

# Sandboxes for your agents

<p class="sw-lead">Run code, control a desktop, or interact with a VR game. Create a sandbox with Python and scale to a pool across your own machines.</p>

Sandweave runs Linux sandboxes without host sudo or KVM. Templates supply the
software and controls; you choose the resources, setup scripts, and lifetime.

<div class="sw-actions" markdown>

[Start with Python](quickstart.md){ .md-button .md-button--primary }
[Connect a cluster](clusters.md){ .md-button }

</div>

## Run your first command

```bash
uv pip install sandweave
```

```python
from sandweave import Sandbox

with Sandbox() as env:
    result = env.run("python -c 'print(2 + 2)'")
    print(result.stdout)
```

This prints `4`. Sandweave prepares the coding template on first use, then
terminates the sandbox when the `with` block ends. The worker needs Linux
x86-64 and Python 3.11+. See [installation](installation.md) for host requirements.

## What do you want to run?

<div class="sw-links" markdown>

[**Code and evaluation →**<span>Run programs, read their output, and give each task an independent sandbox.</span>](commands.md)

[**Desktop agents →**<span>Start GNOME, take screenshots, and send mouse and keyboard actions.</span>](desktop.md)

[**VR games →**<span>Read paired eye images, send controller inputs, and record both views.</span>](vr.md)

[**Distributed workloads →**<span>Connect workers, keep sandboxes ready, and follow activity in the dashboard.</span>](clusters.md)

</div>

## Three things to know

**A template is a recipe.** It defines installed software, startup services, and
controls. Extend a built-in template or provide your own setup script.

**A sandbox is a running environment.** Run commands and interact with its files
and controls. [Save a cache](snapshots.md) when you want to reuse installed software.

**A cluster connects workers.** Weave assigns sandboxes to machines with available
resources. The same `Sandbox(...)` API works locally or with a cluster address.

## Common tasks

| Task | Start here |
| --- | --- |
| Give a sandbox more CPU or memory | [Resources](resources.md) |
| Turn off its internet access | [Networking](networking.md) |
| Keep it running after Python exits | [Lifetime and cleanup](lifecycle.md) |
| Reuse installed packages | [Caches and snapshots](snapshots.md) |
| Keep several sandboxes ready | [Pools and evaluation](pools.md) |
| Find a Python method or CLI command | [Python API](python-api.md) · [CLI](cli.md) |

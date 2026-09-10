# Your first sandbox

After [installing Sandweave](installation.md), create a sandbox and run a command:

```python
from sandweave import Sandbox

with Sandbox() as env:
    result = env.run("python -c 'print(2 + 2)'")
    print(result.stdout)
```

The default coding template includes Python, a shell, and basic tools. It uses
one virtual CPU and 1 GiB of guest memory. The constructor waits until the
sandbox is ready. The `with` block terminates it on exit.

## Work interactively

In a notebook or Python shell, keep a handle to the environment:

```python
from sandweave import Sandbox

env = Sandbox()
env.files.write_text("/workspace/main.py", "print('hello from the sandbox')\n")
result = env.run("python /workspace/main.py")
print(result.stdout)
```

When finished:

```python
env.terminate()
```

`env.close()` disconnects the handle; it does not terminate the environment.
See [lifetime and cleanup](lifecycle.md) for process exits, notebooks, and
detached sandboxes.

## Use your cluster

If you [started a cluster](clusters.md) from this project:

```python
from sandweave import Sandbox

with Sandbox(target="lab") as env:
    print(env.run("python --version").stdout)
```

From another machine, use the **complete HTTP or SSH address printed by the
controller**, including its authentication fragment for HTTP. A cluster name
such as `lab` is local to the project where it was saved.

## Change a setting

```python
from sandweave import Sandbox

with Sandbox(cpu=4, memory="8GiB", network="offline") as env:
    print(env.run("python -c 'print(2 + 2)'").stdout)
```

You can override resources when creating a sandbox without editing its template.
The `network="offline"` setting blocks outgoing network access inside the
sandbox; it does not disconnect the Python client.

## Handle a program error

```python
from sandweave import Sandbox

with Sandbox() as env:
    result = env.run("python -c 'print(2 / 0)'")
    print(result.stderr)
    print(result.returncode)  # 1
```

A nonzero program exit returns a result by default. `check=True` raises a
`CommandError` instead. Connection failures and timeouts still raise exceptions.

[Run commands](commands.md){ .md-button }
[Create a desktop](desktop.md){ .md-button }

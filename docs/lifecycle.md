# Lifetime and cleanup

Use a context manager when the sandbox belongs to one block of work:

```python
from sandweave import Sandbox

with Sandbox() as env:
    print(env.run("python -c 'print(42)'").stdout)
```

Leaving the block terminates the sandbox and discards unsaved state, including
when the block raises an exception. [Cache or snapshot](snapshots.md) before
leaving if you need to retain state.

## What happens when Python exits?

By default, a sandbox follows its creating Python process. It terminates when
that process exits, including a crash or an IPython kernel shutdown. Local
workers track process identity; remote workers allow a 30-second heartbeat grace
period. A prolonged network outage can therefore terminate an attached sandbox.

Interrupting one notebook cell does not end the kernel. Its sandbox can remain
running until you terminate it or shut down the kernel.

## Keep an environment running

```python
from sandweave import Sandbox

env = Sandbox(template="gnome", detached=True)
print(env.id)
env.close()
```

The environment survives this Python process exiting. Save its ID so you can
reconnect later. Detached environments still respect `ttl` and explicit
termination. A `with Sandbox(detached=True)` block still terminates its sandbox
on exit.

## Close, stop, or terminate?

| Method | Effect |
| --- | --- |
| `env.close()` | Disconnect this Python handle. The sandbox's ownership and TTL still apply. |
| `env.terminate()` | Release the runtime and discard unsaved state. Existing caches and external volumes remain. |
| `env.stop()` | Save a checkpoint before releasing the runtime. Return a snapshot reference; if saving fails, preserve the source. |
| `env.pause()` | Suspend the resident sandbox while retaining its memory and GPU state. |
| `env.resume()` | Continue the same paused sandbox. |

`close()` is why an environment can still appear as running in the dashboard
after you disconnect. `terminate()` stops it; the dashboard can retain its record
as terminated history.

## Reconnect

```python
from sandweave import Sandbox

env = Sandbox.connect("SANDBOX_ID", target="lab")
print(env.run("python --version").stdout)
env.terminate()
```

Replace `SANDBOX_ID` with the saved ID and `lab` with the original target. Omit
`target` for a local sandbox. Connecting borrows a handle; it does not transfer
ownership or extend the original lifetime.

Leaving a `with Sandbox.connect(...)` block only disconnects the borrowed handle.
It does not terminate the sandbox.

## Add a lifetime limit

```python
env = Sandbox(detached=True, ttl=300)
```

The five-minute TTL starts at readiness and counts time spent paused or
disconnected. Automatic cleanup does not create a checkpoint.

## CLI lifetime

`sandweave run` creates a sandbox for one command and terminates it afterward.
`sandweave create` creates a detached environment. Use `sandweave stop` to save
and stop it, or `sandweave terminate` to discard unsaved state.

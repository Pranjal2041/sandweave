# Caches and snapshots

Save installed software or files so another sandbox can start from them:

```python
from sandweave import Sandbox

with Sandbox() as env:
    env.files.write_text("/workspace/note.txt", "saved state")
    baseline = env.cache("my-workbench")

with Sandbox(cache="my-workbench") as env:
    print(env.files.read_text("/workspace/note.txt"))
```

Caches save filesystem state by default. A restore starts fresh processes with
independent writable state. It also restores the template's services and controls,
so you do not need to specify the template again.

## Pin a particular version

A cache name can point to a newer revision later. Use the returned reference to
reuse exactly the version you saved:

```python
with Sandbox(cache=baseline) as env:
    print(env.files.read_text("/workspace/note.txt"))
```

A missing cache raises `CacheMiss`. It does not silently build a replacement.
On a cluster, use the same cluster target when creating and restoring the cache.

## Reuse setup automatically

```python
from sandweave import Sandbox

with Sandbox(setup="./install-tools.sh", cache_key="tools-build") as env:
    print(env.run("python --version").stdout)
```

Provide your own setup script. `cache_key` prepares a recipe and reuses its saved
result when the template, script, and declared inputs match. A change to those
inputs creates a new revision. `cache` loads an existing saved state directly.

## Pause and resume

```python
with Sandbox() as env:
    env.pause()
    env.resume()
```

Pause keeps the same environment resident, including memory and GPU state.
It does not release the runtime's memory reservation or save a durable checkpoint.

## Checkpoint memory

```python
from sandweave import Sandbox

with Sandbox() as env:
    env.files.write_text("/workspace/note.txt", "saved state")
    checkpoint = env.snapshot(state="memory")
    if checkpoint.verify()["status"] != "passed":
        raise RuntimeError("Checkpoint verification failed")

with Sandbox(snapshot=checkpoint) as restored:
    print(restored.files.read_text("/workspace/note.txt"))
```

A supported gVisor memory snapshot saves process and kernel state as well as
files. Verification can run asynchronously; the example checks it before
attempting a restore. External services and shared volumes do not roll back.

During gVisor snapshot publication and transfer, Sandweave reuses checksums for
files whose recorded identity and timestamps still match. New copies are checked
before publication. Calling `checkpoint.verify()` explicitly rechecks the bytes.

## Save before stopping

```python
env = Sandbox()
saved = env.stop()
env.close()

with Sandbox(snapshot=saved) as restored:
    print(restored.run("python --version").stdout)
```

`stop()` saves before releasing the runtime. A failed save keeps the source
running. Use `terminate()` when you want to discard unsaved state instead.

## GPU and native runtime support

GPU filesystem caches start fresh GPU processes. Ordinary live graphics state
cannot be restored. CUDA-only memory restoration is experimental and needs
`experimental_gpu_live=True` at capture and restore.

Native Apptainer supports filesystem caches, but not memory snapshots. See
[resources](resources.md#choose-a-runtime) for runtime differences.

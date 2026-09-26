# Installation

Install the Python package in your environment:

=== "uv"

    ```bash
    uv pip install sandweave
    ```

=== "pip"

    ```bash
    python -m pip install sandweave
    ```

## Worker requirements

Workers run on Linux x86-64 with kernel 5.4 or newer and Python 3.11 or newer.
They need permission to run unprivileged containers. Setup installs Apptainer
when needed and checks whether the host permits the actual container launch.
Host sudo and `/dev/kvm` are not required.

Linux 5.4 support requires Sandweave 0.2.27 or newer. Setup downloads a compatible
engine, including when upgrading prepared runtime files from an older release.

Inside an existing container, Sandweave can use container-local root's existing
mount permissions. It reuses that user namespace and prepares the Apptainer
host image once in `host-images` under your chosen Sandweave directory. Guest
code still runs inside gVisor. This does not grant host privileges or override
restrictions imposed on your account by the outer container.

The default Code sandbox reserves 1 GiB for the guest plus 512 MiB for its
runtime. First-time package installation also needs memory for downloads and
unpacking. A 1 GiB host cannot run that default configuration; smaller resource
limits can be used with an already prepared image.

A GPU workload needs a compatible NVIDIA GPU already available to the worker.
Creating a sandbox does not allocate a GPU from a scheduler.

## Prepare on first use

```python
from sandweave import Sandbox

with Sandbox() as env:
    print(env.run("python --version").stdout)
```

The first local sandbox downloads runtime files and installs its template.
Compatible prebuilt engine binaries are used when available; otherwise setup
builds the engine from source. This first preparation needs internet access and
can take several minutes. Later sandboxes reuse the prepared runtime.

To choose storage and prepare a template ahead of time:

```bash
sandweave setup
```

Setup asks what you want to start with and where to store the files. The directory
can be empty. Other templates are installed when you first request them.

## Choose where files go

By default, each project uses `.sandweave` in its current directory. Downloads,
installed runtimes, worker state, and caches go into the storage directory you
select. Sandweave does not search your home or parent directories for a runtime.

```bash
sandweave setup --directory /path/to/sandweave-data
```

Replace the path with a writable directory you own. After successful setup, a
small `.sandweave/location.json` file in the project records your choice.

To share one installation between projects explicitly:

```bash
export SANDWEAVE_HOME=/path/to/sandweave-data
```

Active runtime working directories use separate, temporary worker-local storage.
Keep caches and snapshots on storage that will survive the worker's lifetime.
Worker-local directories honor `SANDWEAVE_LOCAL_DIR`, then `TMPDIR`, then Python's
system temporary-directory default. Set these on the worker before starting it:

```bash
export SANDWEAVE_LOCAL_DIR=/scratch/sandweave
```

Sandweave creates private random directories under that parent for active
filesystems and checkpoint staging. Changing the setting selects a new worker
workspace; it does not move running sandboxes. An invalid explicit directory
raises an error rather than silently using `/tmp`. If the selected filesystem
is tmpfs, its files consume host RAM; choose a disk directory for disk storage.

## Check or repair an installation

```bash
sandweave doctor
```

Doctor shows checks for the selected workload and offers repairs in its terminal
menu. For a check without interactive changes:

```bash
sandweave doctor --check
```

Setup displays progress and saves full logs under `logs/setup` in your selected
storage directory. See [troubleshooting](troubleshooting.md) if a stage fails.

## Upgrade

```bash
uv pip install --upgrade sandweave
```

Restart an existing controller to load updated controller code. Existing running
workers and sandboxes keep the code and runtime files they started with.

## Prepare specific workloads

```bash
sandweave setup --template gnome
sandweave setup --template cuda
sandweave setup --template vr/gunspinning
```

Desktop and VR controls use additional Python packages. First-use preparation
installs those dependencies on a local worker. A client on another machine can
install `sandweave[desktop]` or `sandweave[vr]` to use those controls.

GunSpinning's first installation asks for its Linux game archive. See
[VR games](vr.md) for preparation and recording requirements.

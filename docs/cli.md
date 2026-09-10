# CLI

The CLI uses the same sandbox lifecycle and command semantics as Python.
Use `sandweave --help` or add `--help` to a subcommand for its complete options.

## Installation

```bash
sandweave setup
sandweave doctor
sandweave doctor --check
```

`setup` chooses storage and prepares templates. `doctor` checks the installation
and offers interactive repairs. `--check` only runs checks.

## Run one command

```bash
sandweave run --template coding -- "python -c 'print(2 + 2)'"
```

This creates a sandbox, runs the command, and terminates the sandbox. Output and
exit status pass through to your terminal. Supply one quoted command string
after `--`; the guest shell interprets it.

## Create a persistent desktop

```bash
sandweave create --template gnome --name workbench
sandweave info workbench
sandweave desktop screenshot workbench --output screen.png
```

CLI `create` uses `detached=True`. It leaves the sandbox running until its TTL
expires or you stop or terminate it.

## Pause, save, or terminate

```bash
sandweave pause workbench
sandweave resume workbench
sandweave cache save workbench my-workbench
sandweave terminate workbench
```

To save a checkpoint before stopping, use `sandweave stop workbench` instead of
`terminate`. Restore a named filesystem cache with:

```bash
sandweave create --cache my-workbench --name restored
```

Sandbox IDs also work wherever these examples use a name.

## Clusters

```bash
sandweave cluster start lab --no-worker
sandweave cluster instructions lab
sandweave cluster workers lab
sandweave cluster status lab
```

Startup prints the complete worker join commands and dashboard link. Copy a
printed join command onto each worker. See [connect a cluster](clusters.md).

```bash
sandweave dashboard lab
sandweave cluster stop lab
```

## Cluster pools

```bash
sandweave pool create --target lab --name coding --size 16 --warm 4
sandweave pool status coding --target lab
sandweave pool exec coding --target lab -- "python --version"
sandweave pool update coding --target lab --size 32 --warm 8
sandweave pool close coding --target lab
```

## Jobs

```bash
sandweave job submit --target lab -- "python -c 'print(2 + 2)'"
```

The command prints the job ID. Substitute that ID in these commands:

```bash
sandweave job status JOB_ID --target lab
sandweave job result JOB_ID --target lab
```

## Stereo recording

```bash
sandweave create --template vr/gunspinning --gpu auto --name gunspin
sandweave vr record gunspin --duration 30 --output ./episode
sandweave terminate gunspin
```

The VR template needs prepared game files and an available GPU. Recording exports
both eyes and a synchronized side-by-side preview. See [VR games](vr.md).

# Repository structure

Proposal for review. The [agreed API](../README.md#agreed-public-api-contract-v1)
is unchanged; no implementation or scaffolding accompanies this revision.

**Templates define. Sandboxes run. Capabilities interact.**

| Pillar | Atomic unit | Contract |
| --- | --- | --- |
| `templates/` | One recipe | Describe installation, startup, readiness and attached capabilities. |
| `sandbox/` | One sandbox | Own execution, files, resources, lifecycle and saved state. |
| `capabilities/` | One interaction interface | Attach to a sandbox and supply actions/observations and lifecycle hooks. |

```text
sandweave/
├── README.md
├── pyproject.toml
├── src/sandweave/
│   ├── __init__.py              # Agreed public imports
│   ├── cli.py                   # Calls the same API
│   ├── templates/
│   │   ├── coding/
│   │   ├── gnome/
│   │   └── vr/gunspinning/
│   ├── sandbox/
│   │   ├── sandbox.py           # Sandbox and its lifecycle
│   │   ├── pool.py              # Collections of independent sandboxes
│   │   ├── process.py
│   │   ├── files.py
│   │   ├── snapshots.py
│   │   ├── runtimes/            # gVisor, Apptainer
│   │   └── targets/             # Local, SSH, Slurm
│   └── capabilities/
│       ├── desktop/
│       └── vr/
├── tests/
├── benchmarks/
├── examples/
└── scripts/                    # Build/development/job convenience commands
```

A template is a `template.toml` with optional setup/start scripts. Adding an
application that uses existing controls changes only its template. Built-ins
ship as package data; users can also pass local templates or setup scripts.

A capability implements one action/observation contract. Templates select and
configure it. Desktop and VR use the same extension mechanism available to a
new robotics capability. Domain-specific code stays here; the sandbox supplies
guest execution, file access and lifecycle hooks through a small interface.

Runtime, placement and storage have independent implementations **inside the
sandbox pillar**. The sandbox coordinates them; they do not import individual
applications. Pools reuse that same lifecycle. Slurm allocation logic belongs
in `sandbox/targets/`; helper scripts call it. Native helpers/build recipes stay
beside their owning runtime or capability; generated artifacts stay outside Git.

Put each internal contract beside the module that owns it. Add a shared type or
abstraction when actual callers require it. Local and remote operation share
the same sandbox behavior; persistent workers and channels are execution
mechanisms within this structure. The detailed behavior remains in the
[API contract](sandbox-api-proposal.md), including clean pool state, snapshot
compatibility, readiness and both-eye VR. Those guarantees do not require a
separate top-level pillar for every mechanism.

The test for the structure is change locality: **new app -> template; new
interaction -> capability; new execution platform -> sandbox implementation.**
Create files as behavior is implemented, keeping helpers with their owner.

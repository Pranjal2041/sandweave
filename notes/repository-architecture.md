# Repository structure

**Templates define environments. Sandbox code runs one. Weave manages many.**

The [agreed API](../README.md#agreed-public-api-contract-v1)
remains the public contract; [implementation progress](sdk-implementation-progress.md)
records current acceptance.

```text
src/sandweave/
├── __init__.py
├── cli.py
├── sandbox/               # Execution, files, resources, lifecycle, saved state
│   ├── sandbox.py
│   ├── process.py
│   ├── files.py
│   ├── snapshots.py
│   ├── pool.py
│   ├── runtimes/
│   └── targets.py
├── weave/                 # Placement, durable pools and submitted jobs
│   ├── client.py
│   ├── controller.py
│   ├── scheduler.py
│   ├── state.py
│   ├── pool.py
│   ├── jobs.py
│   └── providers/
└── templates/             # Definition, preparation, startup and controls
    ├── coding/
    ├── gnome/
    └── vr/
        ├── opensaber/
        └── gunspinning/
```

Templates select reusable control implementations using the existing extension
contract. GNOME brings desktop controls; VR templates share VR controls and add
their game's setup/startup. Adding an app with existing controls changes only its
template. Sandbox code handles running instances without application-specific
branches. CLI and pools reuse the same lifecycle.

Native engine sources are currently bundled into the installed package from
the qualified lab scripts. Their workspace and runtime inputs are isolated from
existing lab environments. The wheel must operate without the source checkout;
prepared runtime binaries and images remain external, configured artifacts.

## Distributed management

Weave records requested work and assigns it to existing workers. The scheduler
plans placements; the controller commits them; workers execute them. Commands
and observations go directly to workers. Local pools retain their existing
implementation, while the public `Pool` import selects durable coordination for
a cluster target. See the [guide](weave-usage.md) and [design](weave-design.md).

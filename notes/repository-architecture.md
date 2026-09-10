# Repository structure

**A template is the definition. A sandbox is a running instance.**

The user approved this two-pillar structure. The [agreed API](../README.md#agreed-public-api-contract-v1)
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

## Proposed distributed management

The [Weave proposal](weave-design.md) adds a third responsibility for managing
sandboxes across workers. It is under discussion; the structure and API above
remain the current contract.

# Process visibility and audit

These experimental gVisor options are independent and disabled by default.
They apply to any template, including desktops, and work through local, SSH,
and Weave connections. Update the SDK, controller and participating workers.

```python
from sandweave import Sandbox

env = Sandbox(template="coding", hidepid=2, audit=True)
try:
    result = env.run("id; ls /proc", user="1001:1001")
    report = env.audit_log("episode.jsonl")
    print(report["complete"], report["dropped"])
finally:
    env.terminate()
    env.close()
```

## Process visibility

`hidepid` sets the guest's initial `/proc` mount policy:

| Value | Behavior |
| --- | --- |
| `0` | Existing process visibility. |
| `1` | Other users' process IDs remain listed, but access to their process files is denied. |
| `2` | Other users' process directories are also hidden from listings and direct access. |

Permission checks follow ptrace read rules: matching credentials, dumpability,
and capabilities matter. They apply to cached paths as well as new lookups.
Threads, `/proc/self`, and processes the caller may inspect remain accessible.
This controls `/proc` visibility, not PID namespaces or every possible way of
learning that a process exists.

Run the agent under a separate unprivileged UID. Guest root, `CAP_SYS_PTRACE`,
or permission to become root can bypass this policy. A privileged process can
also mount another procfs. The example's numeric UID does not grant sudo access;
check the permissions of any existing account you choose instead.

## Protected runtime audit

`audit=True` enables gVisor SecCheck events before guest startup. Records are
written to a worker-owned file outside the guest filesystem. Guest processes
cannot edit that file through their filesystem, even as guest root. The worker
administrator remains trusted. This is runtime monitoring, **not Linux auditd
or the `NETLINK_AUDIT` protocol**.

The JSONL stream includes process lifecycle, mappings, rich syscall-exit records
where gVisor has a schema, and raw syscall-exit records. Rich records can contain
paths, flags and results; raw records contain syscall numbers and arguments,
which may be pointer values rather than decoded strings. There is no claim of
complete Linux Audit rule or tamper-detector compatibility. The sidecar lists
the exact trace points and fields collected by the selected runtime.

Records may include command arguments and other sensitive application data.
Worker logs and client exports use mode `0600`. Protect any further copies accordingly.

`env.audit_log(path)` writes the log on the **client**, alongside
`path + ".status.json"`, and returns that status. Async clients can use
`await env.audit_log.aio(path)`. Export before deleting the sandbox.

- `complete=True` means every selected event submitted through that export's
  flush barrier was written successfully. It does not classify an episode as
  clean or cover actions after the barrier.
- `events`, `last_sequence`, `bytes`, and `dropped` describe that exported prefix.
- Queue overflow, serialization failure, disk failure, the byte limit, or an
  unavailable runtime prevent a complete export. Missing data must never be
  treated as evidence that no tampering happened.
- Guest execution does not wait for audit disk writes. The queue is bounded to
  approximately 8 MiB; overflow is counted. Enabling audit adds event-generation
  and storage overhead. Disabled sandboxes do not run an audit writer.

Logs stop growing at 1 GiB by default and subsequent exports report incomplete
coverage. Set a different positive byte limit in the template:

```python
env = Sandbox(
    template={"extends": "coding", "runtime_options": {"audit_max_bytes": 8 * 1024**3}},
    audit=True,
)
```

An export failure never leaves a new sidecar claiming a completed export. Check
both the returned status and your detector's support for the listed coverage.

## Templates, pools and snapshots

Templates may set `runtime_options.hidepid` and `runtime_options.audit`.
Explicit `hidepid=0` or `audit=False` overrides a template or filesystem snapshot.
`Pool(..., hidepid=2, audit=True)` applies the same options to its sandboxes.

Filesystem snapshots retain these settings and start a **new** audit stream on
restore; previous audit records are not included in the snapshot. Audit mode
rejects memory snapshots and live restores because trace continuity across a
live restore is not supported. `state="auto"` uses a filesystem snapshot when
audit is enabled. `hidepid` alone supports memory snapshots, but changing its
value requires a fresh launch or a filesystem restore.

CLI creation uses the same flags:

```bash
sandweave create --hidepid 2 --audit
sandweave create --hidepid 0 --no-audit
```

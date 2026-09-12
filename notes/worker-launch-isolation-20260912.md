# Worker process and storage isolation

Sandweave 0.2.16 separates host process creation from worker storage operations.
The worker must be upgraded and restarted; the SDK API, transport protocol and
engine binary do not change.

## Cause

The threaded worker could hold an image open while starting an unrelated host
command. The child temporarily inherited that handle. Closing it before exec
could enter a filesystem flush and stall process creation. Setting close-on-exec
does not remove this coupling: the inherited handle still has to close.

[FUSE documents flush calls for closes of duplicated descriptors, including
descriptors duplicated by fork](https://github.com/libfuse/libfuse/blob/master/include/fuse.h).
[Python subprocess startup closes unrequested descriptors](https://docs.python.org/3.13/library/subprocess.html#subprocess.Popen).
The fix addresses the worker's process boundary, without checking mount types,
cloud providers or particular paths.

## Implementation

The worker starts one small launcher before storage work or request threads.
The launcher keeps storage files out of its descriptor table. For each request,
it forks a child that receives only the requested stdin/stdout/stderr and
`pass_fds` handles through a private Unix socket. File handles never enter the
launcher itself. The child becomes the command; there is no extra supervisor
process per running command.

The launcher collects exits through SIGCHLD and an event loop. A blocked child
does not hold a launch lock or a server thread. Python's existing `Popen` pipe
and `communicate` implementation remains in the worker; a private adapter
delegates creation, exit status and signals. It preserves descriptor numbers,
working directories, environment, umask, affinity, resource limits, signal
masks, sessions and process groups. Readiness waits use `poll`, which supports
descriptor numbers above 1023.

This adapter is installed only in the worker process. Client Python processes
and guest programs retain their normal subprocess implementation. Worker-side
extensions must use explicit `pass_fds`; inheriting every worker descriptor and
running `preexec_fn` in the threaded worker are rejected. The built-in adapters
use neither option.

## Acceptance

The FUSE tests mount a disposable filesystem whose flush deliberately blocks.
They reproduce the failure with the original `Popen` before testing isolation:

- 128 isolated host commands completed in 89 ms while the original launch
  remained blocked.
- In a real worker on two CPUs, four gVisor sandboxes were created, executed
  commands and terminated in 1.85 seconds while the unrelated flush remained
  blocked. The test releases the filesystem only after those operations finish.
- The test filesystem uses fusepy; it is a test dependency, not an SDK dependency.

`tests/test_worker_launcher.py` covers Python 3.11, 3.13 and 3.14. Coverage includes
streams, Unicode, exec errors, timeouts, concurrent wait/signalling, sessions,
explicit descriptors, changed process settings, more than 1,024 open handles,
concurrent commands, descriptor-pressure recovery and child/descriptor cleanup.

Live acceptance also passed concurrent imports and sixteen leases, pool
retention/cleanup, command/file traffic, native Apptainer operation, and gVisor
filesystem and memory snapshots.

These broader checks exposed two additional bugs: native snapshot import
signatures used paths outside `upper`, and a client could mistake an old worker's
final exit for failure of a new launch. Native signatures now use the correct
paths and inspect guest symlinks without following them on the host. A client
waiting on a previous worker starts one replacement after that worker exits;
a newly started worker's failure still propagates. Both have regression tests.

## Host launch cost

`scripts/profile-worker-launch.py` compared 256 short commands at concurrency 32,
with 128 other commands running. All output and exit codes were checked.

| Path | Serial median | Concurrent commands/s |
| --- | ---: | ---: |
| Ordinary subprocess | 0.81 ms | 2,675 |
| Isolated worker launcher | 2.17 ms | 1,405 |

Isolation adds about 1.36 ms per host process in this measurement. Commands
inside an already-running sandbox use the guest RPC connection and do not pass
through this launcher. These numbers measure host process creation, not sandbox
command RPC throughput. Raw aggregates are in
`notes/worker-launch-isolation-20260912.json`.

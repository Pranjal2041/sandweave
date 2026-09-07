# Experimental CUDA MPS partitions

Implemented and tested on `babel-u5-28`, NVIDIA L40S, driver **610.43.02**, CUDA
driver/MPS tools **13.3**, without KVM, host sudo, GPU resets, or compute-mode
changes. Ordinary GPU sharing remains the default. This is an opt-in CUDA
resource experiment, not a hardware GPU isolation boundary.

## Use

Add these flags to an existing single-GPU launch:

```bash
--gpu 0 --experimental-gpu-sm-chunks 2 --experimental-gpu-client-memory-mib 1024
```

On this L40S, one chunk is four SMs; two chunks give eight SMs. NVIDIA allocates
chunks from the private server's remaining pool and rejects exhaustion. The
driver's final partial chunk can affect rounding; the launcher requests chunks,
not an exact FLOP/s quota. Other cards have not been qualified.

The memory flag is optional. It sets `CUDA_MPS_PINNED_DEVICE_MEM_LIMIT` for
participating CUDA clients, including CUDA context overhead. It does not set
an aggregate environment VRAM cap. Without it, no new MPS client memory ceiling
is configured. Both options are experimental; the SM option requires `--gpu`,
and the memory option requires the SM option. Driver versions other than the
qualified version are rejected before launch because the transport gateway
and native RM observer depend on this driver protocol and ABI.

For a disposable acceptance run, using the existing staged GPU resources:

```bash
cd ~/scratch/general-vm
python scripts/test-mps-live.py --gpu 0
```

This starts temporary environments, validates them, and stops them. It stages
the tracked CUDA probe with ordinary-user read permissions. Its JSON report
is written under `runs/mps-accept-*.json`.

For a persistent environment, add the experimental options before its name
and retain its usual guest setup/boot command. Environment variables are
installed into the initial process, `/etc/environment`, a login profile, and
systemd's default service environment. Fresh ordinary-user logins and systemd
services were tested. Nested Docker must explicitly receive these variables
and the client socket mount through its own setup.

## Runtime path and lifecycle

1. The launcher verifies the allocated `/dev/nvidiaN`, GPU UUID and matching
   host driver through the existing GPU setup. The device minor may differ
   from NVIDIA-SMI's host index.
2. A file lock serializes requests to a private controller under the node-local
   `gvisor/mps-N/` directory. Only experimental environments use this service.
   It runs in static partition mode, restricted to the selected GPU UUID.
3. A small host-only `LD_PRELOAD` observer records successful RM-root allocations
   made by this private NVIDIA MPS server. It forwards every ioctl unchanged.
   The launcher validates the server PID/start time and passes its explicit
   RM-handle allowlist to `runsc --nvproxy-mps-clients`. No handles are guessed
   or copied from unrelated host processes.
4. NVIDIA creates a distinct partition for the environment. A lease records
   its identifier, requested chunks, optional client memory limit, owner,
   gateway and service process identities.
5. The guest sees a read-only mount containing its client gateway socket and
   environment settings. The private MPS management sockets are not mounted.
   The gateway accepts only the observed CUDA connection request, forwards
   the connected CUDA server FD, and completes its acknowledgement. CUDA's
   subsequent traffic bypasses the Python gateway. Management requests and
   unexpected incoming FDs are rejected. Handshakes have timeouts and a
   bounded concurrency limit.
6. gVisor receives the server's sockets/shared-memory FDs and mediates NVIDIA
   calls through nvproxy. Imports are allowed only from explicitly configured
   server RM clients into a destination client registered in this sandbox.
   The NVIDIA driver still validates object-sharing permissions.
7. The launcher checks controller, server and gateway identities every second.
   Failure terminates the affected experimental environment. Startup failures
   do not silently fall back to an unpartitioned launch.
8. Exit, launcher SIGTERM and startup failure clean up owned runtime processes,
   gateway and partition. The last lease stops the private server/controller.
   The first owner's exit preserves a peer's active lease. SIGKILL cannot run
   Python cleanup: a subsequent launch examines stale leases and refuses to
   recycle one while its sandbox control socket still exists. Stop that
   orphaned sandbox before retrying; this is not a background orphan reaper.

The shared native controller/server is deliberately outside each environment's
CPU-control tree and guest memory budget. It retains the launcher's inherited
Slurm CPU pool. Per-environment gateway processes remain in their environment's
CPU accounting. Shared native service overhead is additional to the documented
guest-page/runtime limits; it is not assigned a new aggregate cgroup budget.

## Engine changes and why they were needed

- Imported host Unix sockets can already have `SO_PASSCRED` set. Upstream
  gVisor treated their credentials as file descriptors and returned `EINVAL`
  from `recvmsg`. The repair reserves ancillary space for credentials,
  discards host identities and imports the actual FDs. This reproduced
  [open issue #14594](https://github.com/google/gvisor/issues/14594). Real
  stream, datagram and sequenced-packet socket tests cover the repair.
- Our existing `--directfs=false` path avoids the separate credential-forwarding
  problem described in [issue #14595](https://github.com/google/gvisor/issues/14595).
  CUDA 13.3 uses anonymous shared-memory FDs here; no host `/dev/shm` bind was
  necessary for these tests.
- MPS supplies GPU objects created outside the Sentry. The new explicit
  allowlist permits that import without accepting arbitrary host RM clients.
  Ordinary launches have an empty allowlist. Imported objects remain tied to
  the live native server; they cannot be checkpointed.
- Added `NV83DE_CTRL_CMD_DEBUG_SET_DROP_DEFERRED_RC` (`0x83de0329`), a one-byte
  Boolean control parameter in NVIDIA's 610.43.02 headers.
- MPS context creation also calls `NV2080_CTRL_CMD_GPU_EXEC_REG_OPS`, which
  gVisor gates behind its `profiling` driver capability. Experimental MPS
  launches explicitly enable this capability; ordinary launches do not.
  This expands the GPU driver interface available to an MPS guest and is part
  of its experimental scope.

[PR #13484](https://github.com/google/gvisor/pull/13484) supplied earlier MPS
ioctl support, but did not implement this host-server integration. The changes
above are local engine changes, not claims that upstream has merged a complete
MPS environment feature.

## Acceptance and limits

[Recorded evidence](gpu-evidence/mps-environments.json) covers concurrent
ordinary-user environments seeing four and eight SMs on the same server,
different active partition identifiers, a one-MiB write/readback and a real
PTX CUDA kernel in both, and rejection of a 1,025-MiB allocation with a
1,024-MiB client limit (`CUDA_ERROR_OUT_OF_MEMORY`). NVIDIA-SMI showed about
1.9 GiB physically free during the concurrent boundary checks, so the request
was smaller than available whole-card memory.

The report also covers first-owner exit while the second continues, last-lease
cleanup, server-failure shutdown, and the existing default GPU device/driver
checks. Additional live checks passed for a fresh `ga` login and an ordinary-user
systemd service. The checkpoint wrapper refused the MPS guest before pausing
it. Engine state saving explicitly rejects external MPS clients as well.

NVIDIA-SMI inside the concurrent guests still showed one physical L40S with
46,068 MiB total memory and whole-device utilization/memory counters. Each
process list showed only PID 1, type `M+C`, 426 MiB. PID 1 was the runtime's
attribution (`runuser`), not an accurate list of guest application PIDs.

This mechanism applies to **cooperating CUDA workloads**. A process can unset
the MPS variables and use its normal GPU access. Multiple CUDA clients do not
produce an enforceable aggregate environment memory ceiling. It does not
partition OpenGL/Vulkan rendering, VRAM bandwidth, cache, memory controllers,
or GPU fault domains. Unmanaged host applications remain outside the private
controller and can contend with every partition. Existing Resolve desktops
were preserved and stayed on their original runtimes; Resolve, Earth, Firefox
and games have not been qualified *under MPS*. Their existing ordinary GPU
launch path remains available.

NVIDIA documents the [static partition semantics](https://docs.nvidia.com/deploy/mps/latest/when-to-use-mps.html#static-sm-partitioning)
and [client memory controls](https://docs.nvidia.com/deploy/mps/latest/appendix-tools-and-interface-reference.html#cuda-mps-pinned-device-mem-limit).
The gateway's observed wire protocol and observer ABI require requalification
for a different driver. These acceptance checks establish functionality and
cleanup on this node, not throughput guarantees or hostile-tenant isolation.

CPU-only checks:

```bash
python scripts/test-gvisor-mps.py
python scripts/test-gvisor-gpu.py
scripts/build-gvisor.sh test //runsc/config:config_test \
  //pkg/sentry/socket/unix/transport:transport_test \
  //pkg/sentry/devices/nvproxy:nvproxy_test
```

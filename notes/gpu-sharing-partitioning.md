# GPU sharing and partitioning

Investigated 2026-09-07 on `babel-u5-28`, with engine `b832209`, L40S
`GPU-203c2df2-d777-ba3e-e218-dfc79347f5a5`, driver 610.43.02 and host UID 2710891.
The GPU is `/dev/nvidia0`, host NVML index 3, and logical CUDA device 0 inside
each single-GPU sandbox. Select it by UUID for host NVIDIA commands.

## Current sharing and visibility

Both `resolve-gpu2` and `resolve-optfix` use this same physical GPU through
nvproxy and the host driver. They also share it with a host inference process.
There is no per-environment GPU compute or VRAM limit in the launcher.

Read-only queries at 17:04 UTC found 41,586 MiB total device memory in use.
Each desktop's ordinary-user `nvidia-smi` reported that whole-card total, but
listed only its own 1,068 MiB under PID 1, `/sbin/init`. The host listed the two
Sentry PIDs separately. Direct NVML compute and graphics process queries inside
`resolve-optfix` also returned only PID 1 and 1,068 MiB. The host driver sees the
Sentry as the client, so this is runtime attribution rather than a correct
per-application GPU process list inside the guest.

Aggregate counters therefore reveal other workloads' contributions even when
their process entries are absent. This verifies these particular monitoring
APIs, not absence of metadata leakage through every NVIDIA driver interface.
See [gVisor's GPU architecture and isolation scope](https://gvisor.dev/docs/user_guide/gpu/).

## Hardware and virtual GPU options

- **MIG:** the local device reports current and pending MIG modes as `N/A`.
  NVIDIA explicitly lists [L40S MIG support as No](https://www.nvidia.com/en-us/data-center/l40s/).
  On supported cards, enabling MIG requires host privileges; managing instances
  can subsequently be delegated through capability-device permissions. See
  [NVIDIA's setup guide](https://docs.nvidia.com/datacenter/tesla/mig-user-guide/getting-started-with-mig.html).
  Our nvproxy source also explicitly assumes MIG is unsupported in
  `rmAllocChannelGroup`; changing cards alone would not complete this integration.
- **NVIDIA vGPU:** L40S supports time-sliced graphics vGPUs, but deployment
  requires a host Virtual GPU Manager, a supported virtualization stack and
  applicable licensing. Installing the manager requires host root. The selected
  local PCI device has no `mdev_supported_types` directory. This is not an
  available no-admin setup path on this node. See
  [NVIDIA's installation instructions](https://docs.nvidia.com/vgpu/latest/grid-vgpu-user-guide/installing-configuring-grid-vgpu.html).

## Rootless MPS static partition: native smoke test passed

CUDA 13.1 introduced MPS static SM partitions. See the
[CUDA programming guide](https://docs.nvidia.com/cuda/archive/13.1.0/cuda-programming-guide/04-special-topics/green-contexts.html).
The installed controller reports binary version 13030 and exposes `-S`,
`sm_partition add`, `sm_partition rm` and `lspart`.

A private controller ran as UID 2710891 in the GPU's existing Default compute
mode. It enumerated 142 SMs, created one four-SM partition, and accepted a native
CUDA client. The client reported four SMs and verified every byte of a one-MiB
GPU allocation after filling it with `0x5a`. The partition was removed and the
controller and its server exited. Existing desktops were not attached to MPS.
No sudo, GPU reset, driver change or GPU compute-mode change was used.

[Client probe](../scripts/gpu-mps-client-probe.py) and
[structured evidence](gpu-evidence/mps-partition-client.json) preserve the result.
This is a context/allocation/readback smoke test, not a concurrent kernel
benchmark or a measurement of isolation under contention.

To reproduce, use separate node-local pipe and log directories for a private
MPS controller. Set `CUDA_VISIBLE_DEVICES` to the allocated GPU UUID and set
`CUDA_MPS_PIPE_DIRECTORY` and `CUDA_MPS_LOG_DIRECTORY` consistently. Start
`nvidia-cuda-mps-control -d -S -q`; send `sm_partition add <UUID> 1` to the control
utility and extract the returned `<UUID>/<partition>` identifier. Before the
first client, send `set_default_device_pinned_mem_limit <UUID> 1024M`. Launch
`python scripts/gpu-mps-client-probe.py` with `CUDA_MPS_SM_PARTITION` set to the
identifier. After the client exits, send `sm_partition rm <UUID>/<partition>`,
then `quit` to that private controller. The one-GiB client memory limit was
configured, but allocation rejection at its boundary was not tested.

### NVIDIA-SMI visibility with an active MPS client

The native probe was repeated with `--inspect`, four SMs and a 1,024-MiB client
memory ceiling. It launched `nvidia-smi -i <UUID>` from the client while its CUDA
context and one-MiB allocation remained alive. The result was:

- CUDA reported four SMs and the memory write/readback passed.
- NVIDIA-SMI still displayed the physical L40S and 46,068 MiB total memory,
  with 43,235 MiB used across the card. It did not display a four-SM device,
  the client's memory ceiling, or additional GPUs for the MPS partitions.
- Its GPU utilization was 100%, a whole-device metric rather than a percentage
  of the client's four-SM allocation. The probe performed no sustained workload.
- This driver displayed the native Python client as `M+C` (426 MiB) and the
  MPS server separately (30 MiB), alongside the existing host GPU processes.
  This agrees with the current [MPS quick start](https://docs.nvidia.com/deploy/mps/latest/quick-start.html),
  which describes the `M+C` process type; older descriptions of attribution only
  to the MPS server do not match this driver output.
- CUDA `cuMemGetInfo_v2` returned 45,486.375 MiB total and approximately
  598.692 MiB free. Its total also remained larger than the one-GiB ceiling;
  the free result was consistent with remaining client headroom after context
  overhead. Allocation-limit enforcement still requires a boundary test.

[Recorded output](gpu-evidence/mps-partition-nvidia-smi.json) preserves the exact
values. This was a native MPS client in the host PID namespace, so its process
listing does not establish what would appear after integrating MPS into gVisor.
The earlier non-MPS desktop process-visibility checks remain a separate result.
The temporary partition was removed and its controller/server stopped.

## Scope and remaining integration

Static partitions separate participating MPS clients' compute units. They do
not partition the entire graphics device or provide MIG's memory-bandwidth and
fault isolation. MPS memory limits apply per client, not to an aggregate of all
processes in an environment. NVIDIA documents these
[partition semantics](https://docs.nvidia.com/deploy/mps/latest/when-to-use-mps.html#static-sm-partitioning)
and [client memory limits](https://docs.nvidia.com/deploy/mps/latest/appendix-tools-and-interface-reference.html#cuda-mps-pinned-device-mem-limit).

Our proposed use is CUDA training/inference. Firefox/Earth graphics and Resolve's
complete graphics/CUDA workflow have not been qualified under this mechanism.
Unmanaged host workloads remain outside the private MPS controller and can
still contend for the physical GPU. A partition is not a reserved fraction of
whole-card throughput against every host process.

The engine already includes both ioctl additions from
[gVisor PR #13484](https://github.com/google/gvisor/pull/13484), merged 2026-06-18:
`MPS_COMPUTE` and `NV0080_CTRL_CMD_GR_SET_TPC_PARTITION_MODE`. This is encouraging,
but the native result does not demonstrate clients crossing separate Sentry
instances. Shared MPS transport, resource handles and client identity need a
separate experiment. Environment-wide enforcement would also have to prevent
bypassing MPS or multiplying per-client memory budgets. No MPS launch policy
has been added to the environment runtime.

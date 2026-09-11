# Resources and whole-environment snapshots

Work is isolated in `~/scratch/general-vm`; Gym Anything's main repository is unchanged. The runtime is patched gVisor systrap in unprivileged Apptainer, with `/dev/kvm` absent inside the runtime container. No host sudo or administrator configuration is used. The current node itself has KVM; an actual KVM-less node remains a separate deployment check.

## CPU decision

New launches default to weighted sharing, weight 100, across the inherited Slurm CPU allocation. Affinity defines eligible CPUs, not exclusive ownership. `--guest-cpus 4` describes guest CPU topology/execution concurrency; it does not reserve four physical CPUs or impose an aggregate four-CPU ceiling.

`--cpu-policy weighted --cpu-weight 300` competes for three times the CPU time of weight 100 in the same pool, and borrows idle capacity. `--cpu-policy quota --cpu-quota 1` adds an average one-CPU-equivalent target while allowing execution across the pool. `--cpu-policy shared` keeps normal Linux scheduling without the controller. Each independent broker covers one exact CPU mask; relative fairness requires the same mask. All experiments remain subject to the enclosing Slurm job and other node load.

Host cgroups are root-owned and not delegated here. Host nice is per thread and does not give environment fairness: with four versus sixteen workers, nice 0/5 achieved 0.76:1 rather than the intended 3:1. The new controller accounts host CPU time across each registered process tree and safely pauses guest execution through an idempotent private Sentry RPC. Host SIGSTOP was rejected after it interrupted systrap seccomp notification handling. Startup/restore is excluded until the sandbox reaches running state. Checkpointing temporarily suspends CPU control. A missing controller heartbeat invalidates and stops its experiments.

This is a sampled userspace policy, not kernel `cpu.weight`/`cpu.max`. Guest execution is throttled; runtime/transport CPU is accounted but their background execution is not frozen. Short overshoot is possible. Guest CPU accounting was unsuitable for these tests; results use host counters.

Twenty-second measurements after reducing discovery overhead: equal weights, 4/4 workers: 1.007:1 host CPU; 4/16: 0.901:1; weights 3:1 with 4/16: 2.823:1. One-CPU quotas: 0.9925 and 0.997 CPU. An active environment beside an idle one used 3.901 of four eligible CPUs. Evidence: `runs/gvisor-cpu-broker-optimized.log`, `runs/gvisor-cpu-idle.log`.

Those historical tests did not cover a partially active peer leaving its share
unused. The controller now includes measured demand in allocation, with separate
regressions for intermittent peers, demand increases and quota preservation.
See [CPU demand sharing](cpu-demand-sharing.md) for the algorithm and acceptance.

## Memory

`--memory-mib 8192` enforces an allocator budget for guest pages, including anonymous RAM and writable tmpfs/overlay filesystem data. Restored page ownership is charged before pages load. Reclaimed pages return to the budget. An allocation over budget fails with ENOMEM; an anonymous-memory fault kills the allocating guest process, while the sandbox continues. Live 128 MiB tmpfs and anonymous-allocation tests verify rejection and recovery.

`--runtime-memory-mib 1024` is a separate guard for the Sentry Go runtime, including virtual-kernel metadata. GC aims below that value; a 100 ms sampler forces reclaim and terminates that sandbox if usage remains over budget. A 64 MiB test killed only the stressed sandbox after guest socket creation reached 86,083,864 bytes after reclaim. This demonstrates transient overshoot. New passt and relay processes each have a 512 MiB address-space limit.

These controls do not constitute a kernel-enforced aggregate host RSS ceiling. Host kernel memory, systrap mappings, and reclaimable/shared immutable-image cache are outside the Go guard. Do not sum systrap process RSS/PSS blindly: CLONE_VM processes share address spaces and can be counted repeatedly. The enclosing Slurm memory limit is shared by the job.

## Network

The Ethernet relay enforces policy outside guest control. Default `internet` permits public IPv4 and DNS through the configured resolver, while blocking host addresses, gateway-initiated connections, private/link-local/loopback/multicast ranges and cross-environment host forwards. Host-initiated forwarded TCP connections can receive replies. `--network-policy offline` preserves those incoming forwarded sessions while blocking internet/DNS egress. `--allow-cidr` supplies an explicit extra destination network but cannot override the host-address block.

Nested Docker networks stay inside the virtual kernel, so private Docker-to-Docker traffic remains available. Policy currently supports IPv4; IPv6, VLANs, IPv4 fragments and IP options are rejected at this external boundary. Public DNS/HTTPS, host HTTP forwarding, host canary denial, cross-desktop denial and offline policy were live tested. The original two user desktops retain their earlier runtime/policy until replaced.

## Snapshot implementation and acceptance

Snapshots capture guest processes, memory, writable filesystem data, open files, IPC, internal virtual networks and firewall/NAT state. They retain an exact immutable runtime build and checksums for the EROFS base, OCI spec and launch fixtures. External peers are outside the snapshot and existing external sessions may need application reconnection. Internal established TCP survives in the accepted tests. Acceptance results are recorded in `resource-snapshot-status.json`.

The runtime work covers nftables operation/ruleset serialization, legacy iptables serialization, conntrack reaper synchronization and rehashing, a registry of all network namespaces, interface addresses and routing tables, virtual Ethernet delivery queues, and reattaching external network transport to saved NICs. Dynamic neighbor information is revalidated after restore because host timer callbacks cannot be reused. Queued packets awaiting address resolution are replayed with fresh resolution wait channels.

Commands:

```bash
python scripts/checkpoint-gvisor.py ENV LABEL
python scripts/run-gvisor.py --detach --restore snapshots/LABEL NEW_ENV
```

The checkpoint command saves to local disk first, leaves the source running, then publishes under persistent `snapshots/`. Checksum verification now runs asynchronously after publication; normal restores check sizes and recorded dependency identities without hashing payloads. An unchanged node-local capture is reused when available. Shared immutable dependencies remain under `images/` and `tools/runtime-builds/` and are named in its manifest. `--local-only` is explicitly nondurable and is used for diagnostic iterations. A snapshot is not self-contained if those manifest dependencies are omitted when moving it to another machine. See [asynchronous verification and timing results](asynchronous-snapshot-verification.md) for the updated behavior, explicit verification commands and transfer requirements.

The acceptance probe checks live RAM identity, an open unlinked file and its offset, file ownership/mode, queued abstract Unix-socket bytes, and an established TCP stream across a separate guest network namespace. It mutates the original after saving and checks that the restored clone retains the earlier state. This passed with Moodle, nested Docker/MariaDB and a running Firefox GUI. A second checkpoint of the restored, logged-in desktop also passed the same probe and restored the visible logged-in Moodle course. VNC input opened the participants page successfully after the second restore.

## Recovery and test evidence

The pre-resource prototype is durably saved in `checkpoints/baseline-ae303ca/`: verified git bundle, exact binaries, scripts/notes, evidence and dependency hashes. The base EROFS and exported initial Docker image data are persistent. This recovery bundle is distinct from a running-environment snapshot.

Focused allocator, kernel, network stack, nftables, runtime config and sandbox suites pass; the queued-veth regression passes. The Bazel boot suite encountered a mount-namespace fixture permission failure because its /tmp was mode 0750 and owned by the host UID. The same built boot_test binary passed all tests with TMPDIR=/local/gvisor-public-testtmp (mode 1777); evidence: runs/gvisor-boot-direct-public-tests.log. Failed experimental logs are retained and distinguished from acceptance results in the status file.

## Accepted results and current handoff

- Source: `8c8b1437b27ce0fb61a9db17c5feb33242b7bde3`, branch `experiment/no-kvm-slurm`. Earlier commits isolate baseline compatibility, EROFS ACLs, page budgets, and CPU/runtime guards.
- Exact accepted runtime: `tools/runtime-builds/6690faecc6ff12b743b214b625d3abc2bf1a4b29e67e6a54054a37c69f04b509/`. Runtime binaries are immutable and snapshots pin their hashes. `stage-gvisor.py` builds all five components before publishing: rebuilding runsc alone does not rebuild the separate Sentry binary.
- First accepted full snapshot: `snapshots/snapdocker12-whole`, 4,292,835,269 bytes; checkpoint CLI call 4.316 s; kernel restore 2.341 s. Full automated acceptance: `runs/gvisor/snapdocker12/snapshot-acceptance.json`.
- Recommended snapshot: `snapshots/full-desktop-ready`, taken from that restored desktop after login/navigation; 4,864,196,358 bytes; checkpoint CLI call 6.121 s; next kernel restore 2.856 s. Evidence: `runs/gvisor/ready-clone1/snapshot-acceptance.json`, `snapshot-participants.png`, `snapshot-ready.png`.
- These historical times exclude base/snapshot hashing, durable NFS copying, and launcher setup. The initial launcher performed full verification before restoring; subsequent script changes moved verification out of the normal save/restore path. Kernel timing alone is not launch latency.
- New validated interactive clone: `ready-clone2`, VNC host loopback **47217** on `babel-u5-28`, password `labvnc01`. It uses the new controls and shares CPUs 4–7 for the experiment. Existing `moodle-user1` (40377) and `earth-user1` (40047) remain untouched on their older runtime/policy.
- An offline restore of the same snapshot also passed: public TCP and resolver DNS timed out, host canaries stayed blocked, and forwarded Moodle returned HTTP200 (`runs/gvisor/ready-offline1/network-acceptance.json`).
- Restored guest, outer Docker, and nested MariaDB were denied the host canary. DNS/public HTTPS and host forwards passed. Evidence: `runs/gvisor/snapdocker12-restored/network-acceptance.json`.
- With another environment running 16 busy workers on the same four CPUs, 20 Moodle login-page HTTP requests had 22.5 ms warm median and 134.4 ms maximum. The eight-request idle sample had 36.8 ms median and 67.9 ms maximum. These short samples demonstrate responsiveness, not a claim that contention improves performance. There was no SSH tunnel in these node-local probes, despite the older measurement script's label. Actual Firefox login/course navigation also worked during contention.
- Killing the dedicated quota controller while its systemd guest was paused caused the launcher to stop the entire runtime tree in 3.255 s. Initial cleanup left a FUSE helper behind; tree cleanup fixed it. Evidence: `runs/gvisor/cpu-failure-systemd3/failure-acceptance.json`.
- Nine focused Go suites pass: allocator, kernel, network stack, nftables, veth, TCP, TCP connection tracking, config, and sandbox (`runs/gvisor-four-controls-final-tests.log`). Twelve Python policy/relay/broker checks pass. The full boot binary passes with the documented public TMPDIR fixture.
- Implementation recovery bundle: `checkpoints/resources-snapshots-8c8b143-r3/`; validated running snapshots are separate. Failed snapshots 9–11 remain explicitly marked diagnostic failures. Earlier failures and logs are retained.

Follow-up qualification remains distinct from this completed lab acceptance: run the original Gym Anything environment/task suite, repeat deployment on an actually KVM-less node, and test workloads beyond this Linux subset. CPU weights/quotas and runtime memory guards remain userspace controls, not delegated host cgroups or a claim of a hard total RSS cap.


A final peer-shutdown test found that a closing control socket could terminate the shared CPU broker and stop unrelated controlled experiments. The controller now handles registration-removal races and isolates an RPC failure to its one job; the affected launcher stops its own runtime tree. A deterministic live fault injection replaced one peer's socket with a closed endpoint. That peer exited, the same broker survived, and `ready-clone2` continued serving its state probe and VNC (`runs/gvisor-cpu-peer-failure.log`, `runs/gvisor/ready-clone2/peer-failure-acceptance.json`). `ready-clone1` was retired by the older broker failure; its successful snapshot/VNC acceptance remains valid. Use clone2 for the current interactive session. The first implementation bundle is superseded by the r3 bundle containing this host-controller fix.

Recovery acceptance reconstructs a separate lab from the archive and recorded immutable inputs, with fresh node-local storage, runtime state, network helpers and CPU broker. `scripts/test-recovery.py` verifies hashes, checks out the bundled source, restores the desktop, and checks generic state, Moodle/nested Docker and VNC. Its final result is stored in `runs/gvisor-recovery-acceptance.json` and attached to the r3 recovery bundle.

The clean source-recovery test also found that Git bundle verification did not make the shallow source checkout independently cloneable. The final bundle includes `gvisor.shallow`; recovery initializes a repository with that boundary before fetching the bundle. This reconstructs all lab commits and the exact base/source tree offline. The initial two implementation bundles are superseded by r3.

Final clean recovery PASSED: `runs/gvisor-recovery-acceptance.json`. The archived source reconstructed as commit `8c8b1437b27ce0fb61a9db17c5feb33242b7bde3`; a separate lab using fresh NVMe local storage restored the full probe, Moodle/nested MariaDB and VNC. The temporary recovery guest was then stopped. Current `ready-clone2` and both original user desktops were rechecked and remain reachable.

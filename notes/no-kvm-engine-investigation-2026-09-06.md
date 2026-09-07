# No-KVM engine investigation — 2026-09-06

The objective is to run the existing, full Gym Anything environments quickly on this Slurm cluster without KVM, host sudo, or administrator changes. Linux is the immediate target. Nested Docker, guest sudo and users, desktop applications, networking, and persistent state are requirements. UML is paused by the user. Application substitutions and moving services out of Docker do not meet the objective.

This investigation used read-only host inspection, repository reads, upstream source, and GitHub issues and PRs. No new VM, application, build, namespace probe, or benchmark was launched in this investigation. The only local changes are research notes in this independent lab. Earlier running processes were not audited or stopped; historical process IDs below other notes are not current status observations.

**Finding:** a concrete next engineering candidate is a gVisor-derived Linux runtime using systrap, an internally interpreted filesystem, virtual guest credentials/cgroups, and an unprivileged packet transport. Its constituent mechanisms exist. The complete combination with these environments has not been demonstrated here, and its application speed is unmeasured. A stock `runsc --rootless` command is insufficient. The important result is the narrower engineering problem and the source-level evidence for it.

## Actual host boundary

Observed on the allocated node `babel-u5-28`, real UID/GID `2710891`, kernel `5.14.0-687.25.1.el9_8.x86_64`. This is a vendor kernel with backports; the version number alone does not determine available facilities. These observations do not establish identical configuration across other Slurm nodes.

| Facility | Observed evidence | Consequence |
|---|---|---|
| Unprivileged namespaces | Earlier lab probes successfully created user, mount, and network namespaces; current `user.max_user_namespaces=3092591` | A rootless launcher can use these facilities on this node. No new probe this round. |
| Host credentials | Effective capabilities zero; no matching `/etc/subuid` or `/etc/subgid` entries; both `getsubids` queries failed to obtain ranges | Real host namespaces cannot supply the required collection of guest UIDs through the ordinary mapping path. |
| Mapping helpers | `newuidmap` / `newgidmap` exist with `cap_setuid=ep` / `cap_setgid=ep` | Presence of helpers does not supply an authorized UID allocation. |
| Syscall interception | `ptrace_scope=0`; seccomp actions include `trap`, `trace`, and `user_notif`; process itself had `Seccomp=0` | Selective tracing and systrap have relevant host prerequisites. Device/config presence is not an end-to-end runtime test. |
| FUSE/TUN | `/dev/fuse` and `/dev/net/tun` mode 0666 | Potential unprivileged filesystem/network components. Actual operations still depend on namespace and capability context. |
| Cgroups | Task subtree root-owned 0755, not writable; `cgroup.subtree_control` empty | Cannot build a delegated host cgroup hierarchy for guest systemd/Docker. |
| Task resources | Effective CPUs `0-13,44-57`; task `cpu.max=max 100000`, `memory.max=max` | These leaf values do not prove absence of ancestor limits. Do not equate guest accounting with enforced nested resource limits. |
| Slurm configuration | Read-only `scontrol show config`: `proctrack/cgroup`, `task/cgroup,task/affinity`, `PlugStackConfig=(null)` | Slurm manages this allocation. No configured SPANK container privilege mechanism was established. |
| eBPF | `kernel.unprivileged_bpf_disabled=2` | A design requiring unprivileged eBPF program loading does not fit. Classic seccomp BPF is a separate facility. |
| Low mappings | `vm.mmap_min_addr=65536` | Stock page-zero syscall trampoline schemes do not fit. |
| userfaultfd | `vm.unprivileged_userfaultfd=0`; `/dev/userfaultfd` root-only 0600 | Do not depend on unrestricted userfaultfd. This is not proof that every userfaultfd mode is unavailable. |
| Graphics | NVIDIA open kernel driver `610.43.02`; GPU/render device nodes present | GPU allocation/access and graphics operation remain unverified. Device presence does not establish usable acceleration. |
| KVM | Present on this particular node, excluded by task requirements | A future experiment must hide/deny it and explicitly select systrap. This round did not test a physically KVM-less node. |

Installed user tools include Apptainer, Podman, slirp4netns, pasta, fuse-overlayfs, and Slurm commands. Their presence is useful for construction, not a compatibility result. Slurm's OCI support does not itself grant extra privileges: see [Slurm containers](https://slurm.schedmd.com/containers.html).

## Requirements from the project

A read-only scan found 251 environment definitions: 211 with Linux bases, 29 Windows, and 11 Android. 154 definitions explicitly set `security.use_systemd`. These are configuration counts, not successful environment counts. Docker text appears in numerous installation/setup scripts; a text match alone does not prove Docker is mandatory in each one.

Moodle is an exact acceptance case. Its definition requests the GNOME/systemd base, four CPUs, 8 GiB RAM, networking, guest root, cgroups, and a `ga` account with passwordless sudo. Its setup uses `sudo -u www-data`. Its Compose file runs MariaDB 10.11 with a named volume and `3306:3306` publication; PHP connects through the environment's localhost. The native MariaDB fallback in the script is not an acceptable substitute for this investigation.

Read requirements in:

- `benchmarks/cua_world/environments/moodle_env/env.json`
- `benchmarks/cua_world/environments/moodle_env/scripts/setup_moodle.sh`
- `benchmarks/cua_world/environments/moodle_env/config/docker-compose.yml`
- `docs/content/docs/core/runners.mdx`

Consequently, “the daemon starts,” “a container prints hello,” or “a browser renders a page” is not the acceptance criterion. The same environment must preserve identities, service behavior, bridge networking, published ports, and data across recreation/restart.

## The architectural distinction that changes the investigation

Host UID delegation is required when the host kernel is asked to represent every container user. It is not intrinsically required for a userspace kernel to implement its own credential model. Application instructions can also execute natively without hardware virtualization when privileged kernel behavior is mediated in software.

In current gVisor, `auth.NewRootUserNamespace()` constructs an independent guest identity space, and the loader uses it for the guest kernel. The single host UID mapping is a separate runtime boundary. This is visible in [the credential implementation](https://github.com/google/gvisor/blob/0a1316b0d180600212bd607aa0ccfe2a9b09a899/pkg/sentry/kernel/auth/user_namespace.go#L70) and [the loader](https://github.com/google/gvisor/blob/0a1316b0d180600212bd607aa0ccfe2a9b09a899/runsc/boot/loader.go#L533).

That distinction only works if filesystem behavior agrees. EROFS is read by the Sentry itself; its permission check passes the image's UID, GID, and mode to the virtual VFS permission implementation. It does not require those owners to exist as real host inode owners. See [EROFS permission checks](https://github.com/google/gvisor/blob/0a1316b0d180600212bd607aa0ccfe2a9b09a899/pkg/sentry/fsimpl/erofs/erofs.go#L384). Writable layers must likewise preserve guest ownership internally. Exposing a flattened host directory as the authoritative writable guest filesystem reintroduces the problem.

This makes the following candidate concrete:

```text
Slurm allocation / ordinary host UID
  rootless launcher + lifecycle control
    systrap native application threads
      full desktop, systemd, sudo, actual Docker/Compose
    Sentry guest kernel
      guest credentials and VFS permissions
      EROFS lower + internally managed writable storage
      guest cgroup hierarchy
      guest network namespaces / bridges / NAT
        packet FD -> unprivileged userspace network helper -> host sockets
```

This is a proposed composition of implemented mechanisms plus identified engine work. It is not a completed backend or a claim that all Linux features are implemented.

## What exists, and what must be engineered

**Execution and speed.** Systrap runs application instructions in native host threads. Its source maintains multiple execution threads within a subprocess, unlike the single-thread-per-address-space limitation observed in the paused UML experiment. Common syscall instruction sequences can be patched in memory into jumps to trampolines; uncovered cases still use signal interception. See [systrap](https://github.com/google/gvisor/blob/0a1316b0d180600212bd607aa0ccfe2a9b09a899/pkg/sentry/platform/systrap/README.md), [thread management](https://github.com/google/gvisor/blob/0a1316b0d180600212bd607aa0ccfe2a9b09a899/pkg/sentry/platform/systrap/subprocess.go), and [instruction patching](https://github.com/google/gvisor/blob/0a1316b0d180600212bd607aa0ccfe2a9b09a899/pkg/sentry/platform/systrap/usertrap/usertrap.go).

This avoids two specific mechanisms that hurt UML here: mandatory ptrace round trips for every syscall in the measured configuration, and serialization of concurrent application threads sharing an address space. It does not make guest syscalls free or guarantee GUI performance. Sentry work, copies, scheduling, page faults, and unpatched call sites remain costs. There is no new benchmark result in this report.

**Rootless launch.** The built-in `--rootless` convenience path has documented limitations for `create`, checkpoint/restore, and sandbox networking. Caller-configured namespaces and native rootless OCI paths are different. The launcher must deliberately handle a single authorized host UID, privilege dropping, stdio/control sockets, and lifecycle operations. Do not treat these paths as interchangeable or fix startup by turning on a test-only unsafe mode. See [rootless modes](https://gvisor.dev/docs/user_guide/rootless/).

**Filesystem and persistence.** EROFS lower layers plus internally managed tmpfs upper layers are implemented; disk-backed upper storage and filesystem checkpoints also exist. However, disk backing by itself is not durable filesystem metadata after a crash. Current filesystem snapshots exclude tmpfs mounts created by guest `mount(2)`. That matters because upstream's nested-Docker setup mounts tmpfs on `/var/lib/docker`. A launcher-provisioned, checkpointable data mount or an engine extension must include Docker layers and named-volume metadata, with restart tests proving it. See [filesystem configuration](https://gvisor.dev/docs/user_guide/filesystem/) and [snapshot scope](https://github.com/google/gvisor/blob/0a1316b0d180600212bd607aa0ccfe2a9b09a899/g3doc/user_guide/fs_snapshot.md).

**Guest sudo.** Enable the Sentry's explicit `allow-suid` behavior and the required guest capabilities. Validate SUID transitions, supplementary groups, file ownership, and denied accesses as well as successful sudo. Guest capabilities are not host capabilities. The relevant flag is in [runtime configuration](https://github.com/google/gvisor/blob/0a1316b0d180600212bd607aa0ccfe2a9b09a899/runsc/config/config.go#L454).

**Systemd and cgroups.** The internal cgroup v2 implementation supplies a guest hierarchy independent of host delegation. Recent merged tests boot systemd and exercise service lifecycle. This materially improves the candidate. Resource-limit enforcement inside the sandbox is still a documented gap; successful writes to limit files are insufficient evidence. Hierarchical CPU/memory enforcement requires additional engine work if a task depends on it. [Current compatibility limitations](https://github.com/google/gvisor/blob/0a1316b0d180600212bd607aa0ccfe2a9b09a899/g3doc/user_guide/compatibility.md).

**External network transport.** The existing setup normally discovers/manipulates host network interfaces and opens AF_PACKET sockets. The Sentry's lower-level API already accepts donated FDs, link definitions, and routes. A rootless transport can connect that boundary to a userspace helper instead of requesting initial-host-namespace privileges. Concrete integration points are `runsc/sandbox/network.go`, `runsc/boot/network.go:CreateLinksAndRoutes`, and `pkg/tcpip/link/fdbased`. This needs a configured link, IPs, routes, DNS, inbound forwarding, ownership/cleanup, and correct packet framing. A Unix stream socket cannot simply be substituted for a datagram interface without handling framing. [Existing FD network API](https://github.com/google/gvisor/blob/0a1316b0d180600212bd607aa0ccfe2a9b09a899/runsc/boot/network.go#L174).

Upstream explicitly described work on a UDS/FD transport on September 2, 2026, in [issue #10359](https://github.com/google/gvisor/issues/10359#issuecomment-5515751147). That is evidence of a matching direction, not evidence of a merged implementation. A TAP/slirp arrangement inside an owned user/net namespace is another transport possibility on this host, but its complete runsc launch path was not verified.

**Nested Docker networking.** Current upstream documents actual Docker v27–29 and both bridge and host drivers. However, its setup disables Docker-managed iptables and recommends host networking for port-exposure cases. That does not establish Moodle's unchanged Compose behavior. Preserve the Compose topology and implement or validate the required netfilter operations in the guest kernel. DNS on container loopback, localhost port publishing, bridge isolation, NAT, and MTU behavior all need explicit checks. [Docker-in-gVisor requirements and limitations](https://gvisor.dev/docs/tutorials/docker-in-gvisor/) and [Compose tracking issue](https://github.com/google/gvisor/issues/11937).

**Graphics.** Begin with full desktop/software rendering and native parallel CPU execution. Current nvproxy supports graphics/video beyond CUDA; the exact host driver 610.43.02 appears as a known but unqualified ABI in the inspected source. This is an optional later acceleration lead, not a verified headful OpenGL path. Preserve no-GPU operation because the project includes GPU-zero environments. [GPU support](https://gvisor.dev/docs/user_guide/gpu/) and [driver ABI table](https://github.com/google/gvisor/blob/0a1316b0d180600212bd607aa0ccfe2a9b09a899/pkg/sentry/devices/nvproxy/version.go#L1174).

## PR and issue audit

Statuses were read from the GitHub API on September 6, 2026. “Closed” was checked against `merged`, rather than assumed to mean included in the source.

| Change | Status | Relevance |
|---|---|---|
| [#13359: joinable guest user namespaces](https://github.com/google/gvisor/pull/13359) | Merged June 4 | Fixes user-namespace re-entry used by nested runtimes. Earlier #13323 and #13346 are closed without merge; the final change is #13359. |
| [#13815: guest cgroup2 directories/namespaces](https://github.com/google/gvisor/pull/13815) | Merged August 6 | A guest hierarchy and namespace policy, not host delegation. |
| [#14203: systemd UBI10 integration test](https://github.com/google/gvisor/pull/14203) | Merged August 24 | Boots systemd v257, checks failed units and a transient unit. The upstream test is not proof of our rootless composition. |
| [#14219: in-sandbox-cgroup flag](https://github.com/google/gvisor/pull/14219) | Merged August 25 | Replaces the experimental boolean with explicit guest cgroup configuration. |
| [#13683: guest cgroup.freeze](https://github.com/google/gvisor/pull/13683) | Open | Actual task-stop implementation proposed; not counted as present. |
| [#14047: nftables addrtype](https://github.com/google/gvisor/pull/14047) | Merged August 13 | Implements a rule component used by container networking. |
| [#14049: nftables MASQUERADE compatibility](https://github.com/google/gvisor/pull/14049) | Merged August 15 | NAT support exists in this path; nftables remains behind `TESTONLY-nftables`, so this is not established production parity. |
| [#14604: legacy iptables addrtype](https://github.com/google/gvisor/pull/14604) | Open | A separate implementation path from nftables; cannot conflate their support. |
| [#14606: legacy iptables MASQUERADE](https://github.com/google/gvisor/pull/14606) | Open | Proposed registration/validation around an existing stack target; not yet included. |
| [#14578: rootless Docker runtime path](https://github.com/google/gvisor/pull/14578) | Open | Resolves the executable path earlier. Inspected diff has no regression test; do not assume it solves namespace/lifecycle launch in our direct launcher. |
| [#14579: forwarded GSO/MTU fix](https://github.com/google/gvisor/pull/14579) | Open | Source review found the proposed new successful branch unreachable; details below. |
| [#13944: nested rootless Podman](https://github.com/google/gvisor/issues/13944) | Open | Reports remaining nested networking/storage/namespace issues. Distinguish this from rootless launching of the outer Sentry and rootful Docker inside the guest. |

**A concrete problem in PR #14579.** At inspected head `0b64e6b5db4194f69122912579a7f4440f23a0a9`, both IPv4 and IPv6 call the new GSO helper inside `if packetMustBeFragmented(...)`. That existing predicate requires `pkt.GSOOptions.Type == stack.GSONone`. The new helper immediately returns false for that same value. Therefore, as written, the helper cannot take its successful GSO path from these call sites. This is a static control-flow finding, not a runtime experiment. The patch description claims to resolve the severe forwarding slowdown, but the diff does not establish that. The underlying [issue #14011](https://github.com/google/gvisor/issues/14011) remains relevant; a real fix must address receive/coalescing metadata and eventual segmentation, or keep packets within supported MTU/offload conditions with measured throughput.

## Other mechanisms investigated

**LLNL Pseudopod** is unusually relevant to this exact cluster restriction. It selectively traps identity syscalls and leaves unrelated calls on the native host path. Its source and README explicitly state that virtual IDs do not affect filesystem ownership or permission enforcement; chown/setgroups are faked. Thus it demonstrates a low-interception design, but cannot stand in for full guest credentials plus VFS. Its reported kernel-build timing is about 3.1% above its own baseline, not a prediction for our workloads. [Source and limitations](https://github.com/llnl/pseudopod/tree/a9a6214f1dd9aef8fffacf3b800ecc6765ab3506). The public PR list was inspected; no implementation closing that filesystem gap was identified.

**LKL/uKontainer** supplies real Linux subsystem code and deserves more than an outdated dismissal. [MMU PR #551](https://github.com/lkl/linux/pull/551) was actually merged on January 5, 2025. However, current `switch_mm()` still states that multiple user-mode address spaces are unsupported and performs no switch. The older [multiprocess issue #492](https://github.com/lkl/linux/issues/492) discusses the IPC/process-model work required. The MMU merge therefore does not make current LKL a drop-in full multi-process desktop kernel. [Current source](https://github.com/lkl/linux/blob/2a4f7d0c13276e5c883f941d0109b3f03d739ecd/arch/lkl/include/asm/mmu_context.h).

**VUOS/VirtualSquare** provides a useful partial-virtualization architecture with virtual filesystem/network/identity modules and selective ptrace/seccomp handling. It does not establish this project's systemd/Docker compatibility. Filling all credential, VFS, process, and cgroup interactions would be a separate substantial implementation. [VUOS source](https://github.com/virtualsquare/vuos).

**zpoline** provides fast instruction rewriting but its stock page-zero mapping requirement conflicts with this host's low-address policy. [Issue #16](https://github.com/yasukata/zpoline/issues/16) is still open. Rewriting is a mechanism to reuse in a complete runtime, not an implementation of guest kernel semantics by itself.

**LITESHIELD** describes selective syscall delegation, but the public repository contains no usable runtime source and says release awaits approval. The paper's unsupported privileged-call surface also conflicts with the hard requirements. It is not an available backend. [Public repository](https://github.com/kmanakk1/liteshield), [author paper](https://www.usenix.org/system/files/atc25-manakkal.pdf).

These investigations support reusing an existing userspace kernel with consistent virtual state rather than starting by composing ID-return fakery with native host files. They do not prove there is only one possible architecture. UML remains a separate, paused route with valuable prior functionality evidence.

## Next independent experiment and decision gates

Keep the implementation under `~/scratch/general-vm`, pin the runtime revision, and retain the original project as requirements only. Do not resume the old UML task automatically.

1. Establish one rootless Sentry sandbox with KVM unavailable, an internally owned root filesystem, guest SUID/capabilities, and lifecycle control. Prove UID/GID transitions and permission denials as well as successful operations. Exercise static/raw syscalls, signals, fork/exec, and shared-address-space threads.
2. Prove useful parallel-thread execution and measure syscall/memory/I/O behavior against the same native workload on the same allowed CPUs. Report host wall time and total CPU consumption; native instructions do not guarantee low scheduling cost.
3. Supply external packet transport without host network privilege. Exercise outbound DNS/TLS and host-to-guest high-port forwarding before adding Docker's second network layer.
4. Run actual Docker/Compose with the unchanged MariaDB volume and published-port semantics. Include container-to-container DNS, localhost-to-published-port, data write/read, Compose recreation, and daemon/sandbox restart. A hello-world container or host-network substitution is a failing substitute for this gate.
5. Boot the full systemd desktop and perform real interactions in headful Firefox, Google Earth, and Moodle with browser sandboxing intact. Compare identical software, resolution, rendering mode, and cached/uncached operations; retain failures and require repeatability.
6. Audit task-specific kernel features against the remaining compatibility gaps. Resource-limit tasks need measured enforcement; block-device/custom-driver tasks need additional support. Do not extrapolate three application passes to all 211 Linux definitions.

The first four gates evaluate the general environment engine. If they fail, isolate the missing subsystem and assess the specific engine change before spending another long session tuning an individual application. The current result is a source-backed engineering candidate, not a completed no-KVM solution.

Inspected upstream heads: gVisor `0a1316b0d180600212bd607aa0ccfe2a9b09a899`; Pseudopod `a9a6214f1dd9aef8fffacf3b800ecc6765ab3506`; LKL `2a4f7d0c13276e5c883f941d0109b3f03d739ecd`.

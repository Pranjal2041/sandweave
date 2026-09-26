# Bounded receive memory

Sandweave 0.2.34 selects runtime 2026.09.26.4. Engine commit `43c6d4f00`
changes the generic FD network receive path; it has no application, Docker-image
or destination-specific branches.

## Cause and change

Each flow-hashed FD receive processor previously appended packets to an
unbounded list. During sustained bridge/NAT downloads the receive loop could
outpace the protocol worker. Heap profiles showed retained packet buffers,
backing chunks and list storage growing continuously. Raising the runtime
memory budget only delayed termination. Root-namespace downloads of the same
archive stayed near 180 MB RSS in this reproduction.

Each processor now admits at most 4 MiB of estimated packet memory to its
pending queue. It atomically detaches a batch, processes it outside the queue
mutex, then releases its references and reuses the list storage where possible.
This removes per-packet consumer locking and ensures a delivery pass finishes
even when new traffic keeps arriving. One pending queue and one processing
batch can coexist. Packet memory is estimated with gVisor's `MemSize`; this
bound is not a limit on the entire sentry heap.

Overflow packets are dropped before taking a queue reference, as with a full
NIC receive ring. TCP's retransmission and congestion control recover; UDP may
lose packets under overload. There is no new global lock, periodic queue scan,
application retry shim or increased memory allowance.

The capability `bounded_network_rx` makes new launches upgrade prepared engines
without replacing images. Existing processes and memory snapshots keep their
original engine. A filesystem restore uses the worker's current engine. The
source archive, cumulative patch and fresh compiler output build the release;
Linux 5.4 support and earlier runtime fixes remain included.

## Heap capture

`Sandbox(profiling=True)` opts into gVisor heap capture. `env.profile(path)` or
`await env.profile.aio(path)` writes the profile on the client. The CLI exposes
`create --profiling` and `profile ID --output heap.pprof`, with normal targets.
A borrowed `Sandbox.connect` handle can capture a sandbox enabled at creation.

The worker uses the runtime control socket, independently of the guest agent.
It releases its lifecycle lock before the RPC, saves runsc's binary output in
a private temporary file, returns it through the existing authenticated
transport, and removes staging even if capture fails. Managed credentials are
restricted to their own sandbox. Profiling is disabled by default; enabling it
also permits gVisor's profiling operations/syscalls for that sandbox.

## Acceptance

The original engine, with 2 GiB guest RAM and a 2 GiB runtime allowance, grew
from 172 MiB to 2471 MiB RSS and was terminated by its runtime guard at about
105 seconds. Its sampled live Go heap reached 1040 MiB at 82 seconds.

The candidate completed the same Docker image pull and twelve bulk downloads
in 179.8 seconds. Peak sentry RSS was 624 MiB, ending at 520 MiB; the final live
Go heap was 86 MiB. These are observations on Babel, not universal memory or
throughput guarantees. See the [measurement record](network-receive-memory-acceptance.json).
Raw diagnostic logs and profiles remain in private local artifacts.

Engine tests cover overflow, rejected-packet reference release, recovery after
draining, arrivals during delivery and shutdown during a stalled delivery.
Nine FD/bridge/stack/IPv4/IPv6 runtime test targets pass. The broader `:all`
selection also runs 13 static-analysis targets that fail on existing findings;
the FD static findings were reproduced on the previous engine and normalized
findings match exactly (38 architecture-specific entries, none in the changed
processor files). Those static checks are not represented as passing.

The reusable live Docker regression is `tests/integration/test_network_memory_live.py`.
It monitors RSS and unrelated command latency, collects profiles through sync,
async and CLI calls, and checks the sandbox remains usable. It needs public
registry/download access. `scripts/probe-network-memory.py` accepts a custom
bounded command and captures memory samples and repeated heap profiles.

Live Weave checks passed for a borrowed remote handle, asynchronous capture and
profile capture after memory restore. SDK tests cover opt-in/default behavior,
managed credential scope, binary export and temporary-file cleanup on command
failure, invalid output and timeout. Documentation examples were opened in
Chromium and their copy button exercised.

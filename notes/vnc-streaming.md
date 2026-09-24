# VNC over the SDK transport

`Sandbox.connect(id, target=...).desktop.vnc()` returns an RFB byte stream.
The transport supports sync and async reads/writes, keeps VNC authentication,
and does not start a guest command or expose another network listener.

The client opens a binary WebSocket at the same authenticated HTTP endpoint
used for RPC. SSH uses its existing endpoint tunnel. Streaming sockets have
their own connection pool so an upgrade never occupies a pooled RPC socket.
A single client I/O loop serves synchronous and asynchronous callers.

The worker hands incoming upgrade sockets to an async HTTP handler before
either handler reads any bytes. Existing POST handling remains unchanged.
The stream handler authenticates the worker or sandbox-scoped credential,
checks the template and live runtime state, then connects only to that
sandbox's recorded loopback VNC port. Callers cannot supply a host or port.

Weave forwards between authenticated streams. For an outbound-only worker,
one correlated relay request supplies a random, single-use rendezvous. The
bridge opens its local worker stream and a reverse stream to the controller.
Bytes do not travel through relay polling, JSON responses, disk spools or
the lifecycle executor. Keeping the setup request pending propagates explicit
worker loss to the stream; it is cancelled when the stream ends.

Pumps await socket writes and use bounded 64 KiB binary frames and transport
read queues. Concurrent reads and writes have separate locks on each public
stream. Cancellation, pause, termination, worker loss and connection closure
close both directions. Close-frame writes have an overall deadline as well
as a reply deadline, so a peer that stopped reading cannot hold cleanup open.
Idle streams do not require periodic application reads to stay connected.

Validation lives in `tests/test_vnc_streams.py` and
`tests/integration/test_vnc_stream_live.py`. It covers real socket forwarding,
TLS, sandbox credential isolation, worker loss, cancellation, 64 concurrent
streams, backpressure without blocking unrelated streams/RPCs, and cleanup.
Live acceptance negotiates RFB authentication, reads full GNOME frames and
types into a GTK application through local, SSH, direct Weave and outbound
Weave streams. The screenshots come from those RFB streams, not the separate
desktop screenshot API. No GPU is needed.

Run live acceptance on a disposable worker with `SANDWEAVE_VNC_INTEGRATION`
pointing to its output directory, `SANDWEAVE_HOME` to its private storage, and
`SANDWEAVE_ASSETS` to a prepared GNOME installation. Set `SANDWEAVE_VNC_SSH`
to an SSH address for that worker host to include the SSH path.

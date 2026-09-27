# Network port ownership

Sandweave 0.2.37 fixes the host-port race at sandbox launch. Previously the
launcher bound temporary sockets on port zero, saved the assigned port numbers,
closed the sockets, and started passt with those numbers. Another process could
bind a released port before passt, particularly during concurrent starts.

The patched helper accepts an explicit TCP address with source port zero, such
as `-t 127.0.0.1/0:23799`. It binds and listens once, reads the assigned port with
`getsockname`, and installs the forwarding offset and epoll reference for that
actual port. It keeps the listening socket until it exits. No extra proxy or
launch-wide lock is introduced.

`--tcp-port-report ports.json` writes the selected guest-to-host mapping to a
private, exclusively created file. This completes before the helper exposes its
UNIX listener. The launcher waits for that listener, reads the complete report,
and publishes the existing `runs/gvisor/<id>/ports.json`. Port reporting alone
does not declare a sandbox ready; the guest agent still has to become ready.
Failure or termination closes the helper's sockets through ordinary process
cleanup. Launches and both snapshot restore paths use the same mechanism.

The helper source and both patches ship in the runtime archive. The network
revision changes when either patch changes, so setup replaces old helpers without
changing the gVisor engine or the guest images. Runtime 2026.09.26.6 retains the
engine from 2026.09.26.5.

## Acceptance

Tests run on Babel using unprivileged Apptainer and disposable coding sandboxes:

- 64 simultaneous helpers retained 448 distinct listeners while 1,024 unrelated
  listeners occupied ephemeral ports and another thread continued allocating
  ports. Every advertised port rejected competing binds. All became available
  when their helper exited.
- A helper failing after it bound TCP ports released those ports. An existing
  report file was not overwritten.
- 50 simultaneous SDK sandbox launches all executed their own command. Their
  300 forwarded ports and the control sandbox's six ports were distinct and
  remained owned. A concurrent filesystem restore acquired different ports and
  recovered the saved file.
- During that launch test, 28 commands to the existing control sandbox had a
  median latency of 52 ms and maximum of 819 ms. Launches, command checks and
  the subsequent restore took 11.37 seconds total on this host. These are test
  measurements, not guaranteed startup or command latency.

The regression tests are in `tests/integration/test_network_ports_live.py` and
are included in the default release acceptance command. Test outputs are local
ignored artifacts under `/scratch/pranjala/sandweave-ports-20260926`.

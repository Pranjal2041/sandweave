# Linux 5.4 host support

SDK 0.2.27 selects runtime 2026.09.24.1 for Linux x86-64 workers starting at
kernel 5.4. No host sudo, KVM, alternate container runtime, or application
changes are needed for this compatibility path.

## Engine changes

The previous engine called host `openat2`, introduced in Linux 5.6, without a
fallback in the gofer's parent-directory lookup and self-backed overlay file
creation. It also omitted `ARCH_GET_GS` and `ARCH_SET_GS` from the protected
signal thread's seccomp rules. That omission killed guest execution on hosts
without userspace FSGSBASE, including the tested 5.4 kernel.

The new filesystem helper uses `openat2` on newer hosts. Only `ENOSYS` selects
the fallback, and that result is cached per process. The fallback walks pinned
directory descriptors without following symlinks. Existing overlay files are
pinned before inspection and reopened through their descriptor. Mount IDs
preserve the original prohibition on crossing mounts, including same-filesystem
bind mounts. New files use atomic exclusive creation. The fallback does not
introduce a global lock or data checksums on this path.

The signal filter now permits the GS-base operations from the protected stub,
with the same instruction-address restriction as FS-base operations. Guest
instructions still trap into gVisor. Existing FSGSBASE and newer-kernel fast
paths remain in use where available.

Desktop qualification also exposed a fixed 30-second `systemd-run` timeout
inside the SDK's longer startup budget. Service activation now uses the remaining
startup deadline and requests asynchronous activation; the existing agent ping
still establishes readiness. The control-bus probe respects that same deadline.
The GNOME launcher masks udev's physical-device service, sockets and trigger
units. gVisor supplies virtual devices but does not provide udev's host uevent
socket. Previously, desktop startup depended on repeated failures reaching
systemd's start limit; slow attempts could restart forever and block sysinit.
Closing GNOME's initial overview also uses the remaining desktop-readiness
budget instead of a separate five-second command timeout.

Engine commits: `9e26156` and `ea092f0`. The cumulative patch and immutable
runtime identifiers are recorded in `source-revisions.json`.

## Installation

Runtime descriptors and binary release manifests record their minimum host
kernel. Legacy descriptors imply 5.6. On a 5.4 host, setup upgrades an
incompatible prepared engine while retaining guest images; it never rewrites
the original installation. Source-build fallback includes the same engine
patch. Setup, doctor and automatic local preparation share the supported
kernel range. Remote SDK clients do not need the worker's kernel version.

Gym Anything's doctor fix is proposed in
[PR 67](https://github.com/cmu-l3/gym-anything/pull/67). It calls the installed
SDK's compatibility check for local targets instead of duplicating a version
floor in Gym Anything.

## Reproducing the kernel qualification

The test host is Ubuntu 20.04, kernel `5.4.0-216-generic`, with a non-root
`tester` account. It boots under QEMU TCG solely as test infrastructure. Inside
that host the SDK uses unprivileged Apptainer 1.5.3 and the released systrap
engine. CPU emulation makes its timings unsuitable for native performance
comparisons.

The verified cloud image is
`https://cloud-images.ubuntu.com/focal/20250624/focal-server-cloudimg-amd64.img`,
SHA-256 `18f2977d77dfea1b74aee14533bd21c34f789139e949c57023b7364894b7e5e9`.
`scripts/linux-kernel-vm.py` boots it with an independent 64 GiB overlay,
4 CPUs, a Nehalem CPU model, 16 GiB RAM and loopback SSH. The Code qualification used 8 GiB;
the desktop qualification uses 16 GiB to admit GNOME's default 9 GiB reservation.
Its tools directory contains extracted
Debian `qemu-system-x86`, `qemu-utils`, `seabios`, and `genisoimage` packages and
their libraries. The Debian host SIF supplies their matching libc. No host
package installation or privileged VM launcher is used.

The desktop VM uses Nehalem because this QEMU version's `max` model identifies
itself as a 32-bit AMD processor while advertising 64-bit support. Mesa's LLVM
renderer aborts on that inconsistent model; this is
[QEMU issue 191](https://gitlab.com/qemu-project/qemu/-/issues/191).
Changing the test VM's CPU model requires no sandbox rendering workaround.

Build the release with `scripts/build-runtime-release.py` and package it with
`scripts/package-runtime-release.py`, as described in `runtime-releases.md`.
Install the SDK wheel into a Python 3.12 virtual environment in the test host.
Then run `scripts/accept-runtime-release.py --directory EMPTY --release BUNDLE
--report REPORT.json` from outside the checkout. The optional release argument
serves the exact candidate bundle locally before publication. Omitting it tests
the published GitHub URLs. The prepared GNOME image is built through the same
SDK builder, then passed through normal prepared-runtime installation for the
desktop tests.

For the emulated host, run the desktop tests with
`SANDWEAVE_TEST_STARTUP_TIMEOUT=900 SANDWEAVE_TEST_DESKTOP_TIMEOUT=600`.
These apply the existing public startup and template-readiness settings only in
the test: native tests retain the normal 300-second and 120-second defaults.
GNOME exceeded the default session-start deadline under CPU emulation after
systemd had finished starting; this run is not a native latency qualification.

Raw logs, screenshots and the owned VM receipt for this run are under
`/scratch/pranjala/sandweave-kernel-20260924`. Generated images, keys, binaries
and logs are not included in Git.

## Qualification results

The release was built from a clean upstream source archive plus the committed
patch, using a fresh compiler output directory. Repackaging the completed build
reproduced the candidate archive byte for byte: SHA-256
`63739a9228e86dd897a389cc56cc39b843b9a1cf0f2329e9e7a165903d471bc4`.

On the real Linux 5.4 host, a wheel installed outside the checkout passed fresh
CLI setup, exact engine hashes, absence of compiler installation, CPU overrides,
no `/dev/kvm`, internet access, file operations, pause/resume, independent
filesystem-cache restore, live process-memory restore, offline networking,
cleanup, and preservation of the global storage location. Five filesystem
compatibility tests and eight systrap tests also passed on that kernel.

On Babel's Linux 5.14 host, the same release engine passed 15 SDK, snapshot and
mount integration cases and both desktop integration cases. The SDK host suite
passed 661 tests, with three skipped and 212 integration/GPU cases deselected.
Engine filesystem, gofer and systrap test targets passed as well. These results
do not claim coverage of GPU workloads.

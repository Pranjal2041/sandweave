# Installation source review

Reviewed on 2026-09-09 under the instruction to read and edit without running
the package, installers, builds, tests, probes or sandboxes. Local operations
were file listing, sequential file reads, edits and Git bookkeeping. The tests
added here were reviewed as source and were not executed. Earlier acceptance
results describe earlier revisions.

## Findings and changes

| Finding | Change |
| --- | --- |
| Setup treated an empty destination as a prepared runtime source. | `--directory` and the storage prompt select an output directory. `--assets` selects an optional existing source. |
| A fresh install required private lab images, binaries and snapshots. | The package contains the build scripts, patches and source inputs needed to build the engine and selected guest software from upstream inputs. An existing installation can still be imported. |
| Later commands could stage large files under a small home directory. | Downloads, build output, installed runtimes, worker files and caches use selected storage. A small location pointer makes that choice available from other directories. Slurm placement carries the effective storage paths. |
| The installation could become the saved default before its sandbox check succeeded. | Candidate checks use temporary environment overrides. Saved configuration changes only after the selected template's sandbox passes. Cancellation restores the previous environment values. |
| A damaged import could be reused or repaired in place while another worker used it. | Import verifies manifests and contents. A damaged published installation gets a separate replacement directory. Copy failures do not trigger a source rebuild or fallback into home. |
| Equal-length damaged image copies passed staging checks. | File contents are checked before copies are reused or published. Prepared workers record file identity, length and timestamps to detect subsequent changes. |
| uv environments were assumed to contain pip. | Package installation uses uv with the active interpreter, then that interpreter's pip or ensurepip when needed. Installer caches and temporary files use selected storage. |
| A minimal host was assumed to contain RPM extractors, network helpers, development headers and taskset. | The Apptainer installer gets private extraction adapters; Debian packages supply missing network helpers and dependencies; OCI capability names are declared in the launcher; Python applies CPU affinity. |
| An SDK upgrade could reconnect to a worker using older code. | Worker identities include the SDK and packaged engine sources. |
| Extending a game template could lose its installation dependency. | Built-in templates declare an inherited `installation` name. Setup resolves the required software independently of the extension's name. |
| Workload checks omitted files used later by desktop and VR launchers. | Checks include both desktop bridge files, VirtualGL, the graphics shim and the GunSpinning dependencies. The selected workload receives the final sandbox check. |
| Named keyboard input required X11 on the host. | Desktop installation supplies X11 key definitions as data. Keyboard translation reads those definitions without loading a host graphics library. The setup sandbox check exercises named input and a subsequent screenshot. |
| Host NVIDIA metadata paths were distribution-specific. | Driver staging reads installed libraries and generates descriptors using guest paths. Compute-only workers do not require graphics libraries; graphics templates check them explicitly. |
| A native restore could substitute the current base image for its saved one. | Native images are addressed by content; snapshots retain and verify their recorded base. |
| Large CPU allocations could exceed the filesystem component-length limit. | CPU broker directory names use a digest while registrations retain the actual CPU list. |

Setup serializes work within the selected directory. Downloads use temporary
files and verified receipts. Completed runtime inputs are available on retry;
failed build work and logs are retained at the printed path. Setup preserves
existing configuration, targets, jobs, desktops and snapshots.

## Review coverage

The source review covered package metadata and wheel inputs; the public API,
CLI and onboarding; sandbox, worker, connection, resource, pool, file, process,
snapshot and placement modules; both runtime adapters; every built-in template,
setup script and control provider; the explicit engine input list and its
launch, lifecycle, network, CPU, GPU, filesystem and VR dependencies; the new
installers and archive readers; the bundled gVisor, Monado and Primus patches;
SDK tests and integration cases; the point-mass extension; and the README,
usage guide and relevant runtime/build notes.

The added regression cases cover empty destinations, preserving configuration
on failed or interrupted checks, publication order, corrupt imports, same-size
image damage, template inheritance, missing graphics helpers, keyboard input
without host X11 and archive path
escapes. Existing staging tests also cover interrupted and short copies. Git
diff and status checks are the executable-independent checks for this change.

The README and usage guide received a separate language audit. The API examples
retain the agreed command-string contract.

## Upstream inputs checked

The installer arguments and generated wrappers were read from the pinned
[Apptainer installer](https://raw.githubusercontent.com/apptainer/apptainer/v1.5.3/tools/install-unprivileged.sh).
The Ubuntu base checksum was checked against its
[release manifest](https://cdimage.ubuntu.com/ubuntu-base/releases/22.04/release/SHA256SUMS).
The image conversion flags were checked against the
[EROFS manual](https://raw.githubusercontent.com/erofs/erofs-utils/master/man/mkfs.erofs.1).
Helper dependency handling follows the Debian package metadata for
[erofs-utils](https://packages.debian.org/trixie/erofs-utils),
[passt](https://packages.debian.org/trixie/passt) and
[iproute2](https://packages.debian.org/trixie/iproute2); the installed `prlimit`
path is listed in [util-linux](https://packages.debian.org/trixie/amd64/util-linux/filelist).
The keyboard definition files are supplied by
[x11proto-dev](https://packages.debian.org/trixie/all/x11proto-dev/filelist).
The runtime source revision and cumulative patch remain recorded in
[`source-revisions.json`](source-revisions.json).

## First-use template installation

The reported `Sandbox(template="gnome")` failure reached the desktop bridge
after selecting an installation containing only coding inputs. Construction
had no dependency-preparation step. The bridge then exposed a lab build command.

Local constructors now resolve the recipe and its resource options, prepare
missing dependencies, and only then choose a worker. Python pools and CLI
creation share this path; named CLI pool creation also prepares before choosing
its worker. Inherited templates keep their built-in installation dependency.
Plain SSH worker startup passes that dependency to preparation on the worker.
Explicit remote-worker endpoints and Slurm workers retain their existing
prepared-worker contract.

Automatic preparation uses the shared setup implementation in a subprocess.
It honors selected storage, uses `.sandweave` in the current directory when no
storage was selected, serializes installation with setup, and rechecks after
waiting for the lock. It checks files and software, then lets the requested
sandbox perform launch and control-readiness checks with the caller's actual
resource settings. Interactive setup retains its disposable acceptance check;
a scoped marker prevents that check from recursively starting installation.

The launcher pins the selected directory and immutable runtime path in the
new worker's environment. Worker staging consumes that exact path, even if
another constructor installs a different template concurrently. An explicit
legacy source can have a managed extension recorded for subsequent connections;
the original source and the caller's environment remain unchanged. Reuse checks
presence and manifests without hashing entire images. Installation and initial
staging retain content verification. Progress is written to stderr and a private
installation log, preserving CLI stdout for results.

Source-reviewed regression cases cover adding GNOME after coding, inherited
templates, first use without setup, reuse from another directory, Python
dependencies, failed installation before worker launch, cheap reuse checks,
source overrides, setup recursion, concurrent-install rechecking, and progress
output. No package code, installer, build, probe, sandbox or test was executed
for this change. The earlier runtime results are not results for this revision.

# Registry image support

The image is a filesystem base; the template is a recipe. Both can be supplied
to `Sandbox`, and an explicit `image=` overrides the template's default image.
Existing templates keep their behavior when no image is supplied. See
[the image guide](../docs/images.md) for the public contract.

## Implementation

- `templates/registry.py` resolves public HTTPS registry manifests, selects
  Linux amd64, and verifies manifest/config/blob digests and sizes.
- `templates/layers.py` merges layer metadata without extracting guest paths
  or ownership onto the host. It handles whiteouts, opaque directories, hard
  links, symlink parents, ownership, modes and extended attributes.
- `templates/images.py` adds a private static control interpreter, packs the
  merged filesystem with the existing EROFS builder, and publishes a verified
  immutable base. Cache identity includes the manifest digest, interpreter
  digest and importer format. Aliases, blobs and images use the selected
  Sandweave storage directory; publication and concurrent preparation use locks.

The worker resolves ENV, USER and WORKDIR before setup/cache fingerprinting.
Snapshots carry the prepared definition and filesystem; cluster pool members
inherit the builder's prepared definition. The original recipe still supplies
lease policy such as TTL. ENTRYPOINT and CMD remain inspectable metadata;
templates own startup services. Private registry authentication, sparse tar,
Windows and other architectures are outside this implementation.

No Docker daemon or new isolation engine is introduced. Guest code runs inside
the existing gVisor runtime. Image support does not supply a Docker API adapter
for evaluation harnesses or change gVisor's kernel compatibility.

## Control interpreter

The pinned distribution is Astral python-build-standalone's
`cpython-3.13.15+20260901-x86_64-unknown-linux-musl-lto+static-full.tar.zst`:

`81937f0eb62b3c8440543ffb0e3453d5ef3daf671c5344d4acc41207535ed84d`

The full static archive is necessary: the musl `install_only` artifact still
requires a dynamic loader. The importer verifies the ELF architecture and
rejects PT_INTERP/PT_DYNAMIC headers. Only the interpreter, standard library
and upstream license/metadata files are retained under `/.sandweave-runtime`.
Guest Python binaries and libraries remain intact. The agent uses isolated
Python startup (`-I -S`) and its own process spool.

Upstream: [release](https://github.com/astral-sh/python-build-standalone/releases/tag/20260901),
[distribution documentation](https://github.com/astral-sh/python-build-standalone/blob/main/docs/running.rst).

## Acceptance, 2026-09-10

Tests used disposable CPU workers on Babel, without sudo or KVM. The prepared
coding installation was explicitly selected through `SANDWEAVE_ASSETS`; the
SDK did not discover another installation. Logs and large artifacts are retained
under ignored `runs/oci-acceptance` and `runs/oci-weave-*` directories.

- Host suite: **250 passed, 4 skipped**, with integration/GPU tests deselected.
  Cases cover layer transformations, invalid paths/digests, root metadata,
  image/template precedence, resolved pool definitions, cache reuse, worker
  preparation recovery, CPU broker acknowledgment and locked runtime state.
- `test_oci_images_live.py`: **7 passed**. BusyBox without Python, Python 3.12
  slim, and nonroot distroless Python without a shell all ran. Checks include
  numeric user/group overrides, workdir, setup, startup services, command/file
  I/O, offline networking, filesystem caches, pristine pool leases, pause/resume
  and memory snapshots preserving a live process and its stdin.
- `test_swe_image.py`: **passed**, running **219 upstream Biopython tests** in
  an official SWE-rebench task image. This is application/image acceptance,
  not a complete evaluation harness or task grading run.
- `test_weave_live.py -k docker_image`: **passed** on two isolated workers.
  Draining the source forced an image snapshot onto the other worker; its files
  and digest survived transfer. Managed pool leases and a job with uploaded
  input preserved stdout, stderr and exit code 7. The accepted run is recorded
  in `runs/oci-weave-verified.xml`. An earlier run exposed the unresolved pool
  definition bug; that failed run is retained under `runs/oci-weave-final`.

Selected Linux manifest digests:

| Image | SHA-256 |
| --- | --- |
| `busybox:1.37.0` | `7a3ebe5bfd1a4a19797d20b0c0bb39d44393e9a03fd852c0865b0f540d868df0` |
| `python:3.12-slim` | `2fe5997d249a808b8eeea52c58a1dbffbba28754dc11699ef5c029f2d818ce79` |
| `gcr.io/distroless/python3-debian12:nonroot` | `0f8ca62dea61023c1fe02e445bd154ee02b2d97aec377f965ae3c641ec838e61` |
| `swerebench/sweb.eval.x86_64.biopython_1776_biopython-5005` | `5c44c88cfe52bb11bdcd25535035194e2914873192dcd6e0a10d7e13893ca3ae` |

Biopython revision: `11728693973fafcce459cf65c0450d579fd81b18`.
The executed command was `python -m pytest test_seq.py test_Seq_objs.py -q`,
from `/testbed/Tests` after activating the image's `testbed` Conda environment.

Three repeated cached launches measured the complete `Sandbox(...)` call:

| Base | Wall time, seconds | Median |
| --- | --- | --- |
| Existing coding template | 1.430, 0.900, 1.353 | 1.353 |
| BusyBox | 1.975, 1.898, 2.066 | 1.975 |
| Python 3.12 slim | 1.755, 2.206, 2.424 | 2.206 |

These are small samples on shared hardware/storage, after image preparation and
worker startup. They are not cold-download times or general performance
guarantees. Three leases from an already prepared local pool took 0.068, 0.026
and 0.021 milliseconds to acquire; that excludes building/refilling the pool and
executing commands. Image preparation is reported separately in `env.timings`.

Runtime acceptance exposed two existing startup races: the watchdog could run
before CPU broker registration was acknowledged, and the status reader could
parse runsc's state during a write. The fixes wait for registration and use
runsc's existing state lock. Slow worker staging also retains the preparing
process across client interruption rather than starting a duplicate on retry.

The documentation passed a strict build, link/example checks (22 pages,
76 Python examples, 1,438 internal links), and desktop/mobile browser checks.
Rendered image guide screenshots were inspected from
`runs/docs-images-acceptance`.

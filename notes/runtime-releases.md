# Runtime releases

Sandweave 0.1.1 selects a pinned engine from GitHub Releases before building
one locally. The Python API and `sandweave setup` share this installation path.
Existing configured installations keep their recorded runtime files.

## Selection and fallback

Before the engine download, setup checks Linux x86-64, kernel 5.6 or newer,
CPU instruction flags, and a namespace/seccomp probe through the installed
Apptainer. The probe uses a minimal temporary root and the resolved host Python
interpreter. It needs no downloaded container image and handles virtual
environments under `/tmp` and symlinked Python installations.

The SDK pins a manifest URL and SHA-256. That manifest identifies each archive's
architecture, minimum kernel, CPU flags, size and SHA-256, plus the engine
revision and patch checksum. Selection checks the patch against the SDK's
bundled source. Extraction rejects escaping paths, unsafe entries and an
expanded size larger than declared. Installation verifies the recorded files
and executable permissions before publishing the directory.

No matching artifact or an unavailable release selects the source builder.
An integrity failure stops installation. A source build cannot solve an
unsupported architecture, an old kernel or a host permission restriction.
`sandweave setup --build` explicitly selects a new source build.

Archives contain the five static engine executables, their manifests, the
engine patch, build provenance and upstream notices. They do not contain a
compiler image, Linux guest images, GPU drivers, NVIDIA utilities or games.
Setup retrieves its small host image, runtime utilities and selected template
software separately. Code setup therefore avoids the compiler image and Bazel.

## Qualification: 2026.09.09.1

The engine was built from a fresh source tree and Bazel output directory on
Babel, using the pinned upstream compiler image. Its upstream revision is
`0a1316b0d180600212bd607aa0ccfe2a9b09a899`; the patch is the same cumulative
no-KVM patch already used by this checkout. No engine source changed for this
release. `file` identifies all five executables as static x86-64 binaries;
the C prewarmer is freestanding and links no libc.

The same executable hashes passed on both machines:

| Worker | CPU | Kernel | First Code setup | Ready sandbox |
| --- | --- | --- | --- | --- |
| Babel | AMD EPYC 9354 | 5.14.0-687.25.1.el9_8 | 70.0 s | 0.67 s |
| Orchard Flame | Intel Xeon Platinum 8481C | 6.17.0-1016-gcp | 180.8 s | 1.44 s |

These initial measurements used fresh installed wheels and empty storage,
with candidate archives served over local HTTP before publication. Babel used
local NVMe storage; Flame used `/project/flame` NFS. Apptainer was already
installed. They include upstream template downloads and the setup sandbox check,
but exclude downloading the engine across the public internet. They are
individual observations, not a cross-machine performance guarantee.

Both runs checked CPU overrides, absence of `/dev/kvm`, commands, files, internet
access, offline networking, pause/resume, independent filesystem-cache restore,
and a memory snapshot of a running Python process. The restored process retained
its random in-memory value and accepted independent stdin. Neither run downloaded
the compiler image or invoked Bazel. Explicit storage left the user's global
location unchanged; test sandboxes and their idle workers were released.

On Babel, automatic first use extended that installation to GNOME. The two
desktop acceptance cases passed: keyboard/mouse input, clean startup, pause/resume,
filesystem restore, resolution overrides and overview-state preservation. The
1920×1080 initial and application screenshots were opened and inspected. The
default font set lacks Japanese glyphs, although the application received the
correct Unicode text. This release does not change desktop fonts or GPU support.

Host tests cover selection, unavailable-release fallback, forced source builds,
download corruption, cached-file corruption, extraction limits, blocked hosts
and the two Python path cases found during qualification. The wheel and source
distribution pass strict metadata checks; rebuilding the source distribution
produces identical wheel file contents.

## Build and publish another release

Use an ordinary checkout and a fresh build directory. Local scratch avoids
placing Bazel's many temporary files on NFS. The optional image cache supplies
trusted SIFs without reusing source files or compiler output.

```bash
python scripts/build-runtime-release.py \
  --directory /path/to/empty-build \
  --result /path/to/build-result.json

python scripts/collect-runtime-notices.py \
  --source-archive /path/to/empty-build/downloads/gvisor-REVISION.tar.gz \
  --output /path/to/notices

python scripts/package-runtime-release.py \
  --build-result /path/to/build-result.json \
  --notices /path/to/notices \
  --output /path/to/release \
  --version YYYY.MM.DD.N
```

The packager fixes archive order, ownership, modes and timestamps. Repackaging
the same inputs produces the same bytes. It refuses to overwrite existing
release outputs. The notices collector records the pinned Go module versions
and downloaded archive hashes, including modules used only by build tools/tests.

Install a wheel into an isolated environment outside the source checkout, then
run the acceptance script with new storage on each qualification machine:

```bash
python /path/to/checkout/scripts/accept-runtime-release.py \
  --directory /path/to/empty-test-storage \
  --release /path/to/release \
  --report /path/to/acceptance.json
```

`--release` temporarily replaces only that test wheel's release pin with a local
HTTP manifest. It preserves the archive bytes and restores the original pin.
After publication, omit `--release` to test the shipped GitHub URLs without
modifying the installation. Use a new storage directory for that check too.

Copy the generated `runtime-release.json` into `src/sandweave`, build and check
the Python distributions, and commit the source and evidence. Publish the engine
archive, `manifest.json`, `runtime-release.json` and `SHA256SUMS` under the exact
`runtime-vYYYY.MM.DD.N` tag referenced by the generated URLs. Verify anonymous
downloads before publishing the SDK to PyPI. Never replace assets belonging to
an SDK-pinned release; use a new runtime version and SDK pin.

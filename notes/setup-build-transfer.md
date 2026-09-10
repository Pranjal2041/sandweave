# Setup artifact transfer

Setup previously mounted its host output directory writable inside the build
guest. Creating the final tar archive went through gVisor's file server, which
applies the guest's requested ownership to the newly created host file. That
coupled a build's success to the destination's ownership and the runtime's
UID/GID mappings. A fresh installation in a private local directory did not
exercise inherited host group ownership.

The builder now mounts only its read-only input directory. It installs software
and compiles helper binaries inside the guest. At completion, it streams the
root filesystem and helper archives over stdout, with installation logs on
stderr. The host launcher writes the archives in the selected storage directory.
No guest process creates or changes ownership of a host output file.

The transfer uses bounded chunks, a SHA-256 digest for each archive, and a final
completion record. The receiver keeps output in a private temporary directory
under the destination's parent. It publishes the output only after the complete
transfer and a successful guest exit. Exceptions and interruption remove this
unpublished transfer directory. The surrounding failed build and logs remain
available for diagnosis.

Numeric ownership, modes, ACLs and extended attributes stay in the rootfs tar
headers. The archive is converted directly to EROFS; its files are never
extracted onto the host. Helper files are extracted using the host identity.
Their executable bits are retained; guest ownership, setuid/setgid bits and
ACLs are not applied to host files. Paths are checked, and symlinks are created
after regular files so extraction cannot write through them.

## Acceptance, 2026-09-10

Evidence: `runs/build-transfer-P9DvROgF/` and
`runs/build-transfer-host.xml` (ignored generated artifacts).

- A fresh coding setup downloaded its runtime and built its guest from Ubuntu
  Base in a new NFS directory inheriting a supplementary group. Image export,
  image conversion, worker staging and the public coding sandbox check passed.
  Guest installation/export took 40 seconds; image conversion took 12 seconds.
  The separate Apptainer image pull/conversion took 6 minutes 14 seconds.
- A GNOME build from the earlier release test's coding image exported its
  compiled bridge and library, produced an image, started GNOME, advertised
  VNC and returned a 1920×1080 screenshot. The complete test took 130.67 seconds.
  This was desktop build/startup acceptance, not a VR gameplay test.
- Six real guest transfer cases passed on local storage: private ownership,
  inherited group ownership and default ACLs, each with a successful guest
  and a guest exiting nonzero after sending a complete archive. Four equivalent
  private/shared-group cases passed on NFS. Two default-ACL cases were skipped
  there because this mount cannot create POSIX default ACLs; those cases passed
  on local storage. The first test run exposed that fixture limitation before
  launching its ACL cases.
- A real interrupted transfer left no published output or live recorded
  launcher helpers. Stream tests also reject truncation, corruption and
  oversized chunks, and test helper link/path handling and guest metadata.
- The host suite passed 220 tests, with four optional cases skipped. A subsequent
  focused run of artifact and deployment tests passed all 28 cases.

The release command now also runs the transfer matrix against its installed
wheel and the runtime built by that wheel. Filesystem-specific acceptance can
use `tests/integration/test_build_transfer.py` with `SANDWEAVE_TRANSFER_ASSETS`
pointing to an explicitly prepared coding runtime and pytest's `--basetemp`
pointing to a new disposable directory on the filesystem under test.

No existing user installation, test directory or desktop was changed. The
engine binary and its independent source checkout are unchanged.

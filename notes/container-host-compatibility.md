# Running inside another container

The compatibility path keeps Apptainer, gVisor, and the existing worker
architecture. It uses permissions the caller already has; it neither requests
host root nor changes host AppArmor, seccomp, mounts, or KVM configuration.

## Failure mechanisms and changes

- Optional Apptainer bind sources such as `/etc/localtime` can be absent in a
  minimal host image. Skip missing optional sources instead of requiring a
  particular host filesystem layout.
- A container-local root with `CAP_SYS_ADMIN` may already have the needed mount
  authority. An additional user namespace can make proc/sys mounts unavailable.
  Avoid requesting another Apptainer user namespace in this case. For gVisor,
  reuse the current user namespace only when its UID mapping shows that the
  caller is already namespaced. Ordinary host users retain their existing path.
- Compressed Apptainer images rely on nested FUSE mounts that an outer runtime
  may not propagate correctly. Container-local root uses Apptainer's existing
  directory-image support. Prepare the trusted host image once, publish it
  atomically, and reuse it across launches in the chosen storage directory.
- gVisor honors explicit OCI user namespace paths. Joining the current user
  namespace is skipped only after comparing the open namespace descriptors;
  Linux rejects re-entering that namespace even for root.
- A mediated mount can reject an alternate procfs descriptor path. The fallback
  uses the canonical descriptor path only after checking that it names the same
  device and inode. It does not fall back to an unpinned destination pathname.
- An outer runtime can protect the literal `/proc` mountpoint after a private
  pivot. On `EBUSY`, remove the gofer's private descriptor bind, relocate its
  private proc mount, then detach it. The existing check that `/proc/self` is
  inaccessible remains mandatory.
- Linux rejects a second seccomp notification listener with `EBUSY`. gVisor
  falls back to its existing futex communication in that case; its isolation
  filters and the outer filter remain installed. Other errors still fail.

The namespace choice is recomputed on restore, so a saved OCI path or UID
mapping is not blindly carried to another worker.

## Acceptance on 2026-09-23

- Babel, Linux 5.14, ordinary unprivileged account: clean Code installation from
  the candidate binary release; commands, internet, files, pause/resume,
  filesystem checkpoints, live-process restore, offline networking and cleanup.
  Initial setup took 71 seconds; the next sandbox became ready in 1.20 seconds.
- The same Babel runtime under an inherited allow-all seccomp notification
  listener: concurrent guests, isolation, process checkpoints, network access,
  offline networking, pause/resume and filesystem restore all passed.
  `scripts/with-seccomp-listener.py` reproduces this kernel constraint without
  requiring a particular container provider.
- Daytona, Ubuntu 22.04 container on Linux 6.8, existing container-local root:
  the same installed-wheel API checks passed using the Code image prepared on
  Babel. Tests used 256 MiB guest plus 128 MiB runtime reservations, including
  concurrent sandboxes and a live-process checkpoint restored into a second
  sandbox. Apptainer 1.5.3 was unchanged; no host policy changes were made.
- Fresh Daytona setup passed namespace/seccomp checks, image preparation and
  guest startup. Package installation then saturated the instance's hard
  1 GiB memory limit with no swap. The owned test was stopped and its processes
  were reclaimed. This is not recorded as a successful clean installation.
  Default Code admission separately requires 1 GiB guest plus 512 MiB runtime,
  exceeding that instance's limit. A clean-install rerun awaits additional RAM.

The earlier ordinary-user Daytona login was restricted when writing its new
user namespace's `setgroups` file. That account restriction is distinct from
the root-container failures above. These fixes do not grant permissions an
account lacks. Its older SSH login expired before final acceptance.

Engine revision: `9bb32ef9cd87a8c01bb663e3525ba23ff830ae03`. The engine's
`specutils_test` passed, including same-namespace aliases. Python regressions
cover capability selection, missing bind sources, restored UID mappings, and
concurrent or failed host-image extraction. The installed-package acceptance
is `tests/integration/test_container_host.py`.

# OSWorld benchmark acceptance

Recorded on 2026-09-14. This qualifies the specified desktop and evaluator paths;
it does not claim that gVisor implements every service of a Linux VM.

## Inputs

| Input | Pin |
| --- | --- |
| Read-only `cua-speed-run` reference | `681f8dbc695ff3a7e3af2f532bec982725818211` |
| Canonical OSWorld evaluator | `315a7603173feadf1b8a85cbc006c93ffe1dc1a1` |
| Official image repository | `xlangai/ubuntu_osworld`, revision `a5d9c3eaae98eebf6e3a0beb84e7e47cf72ae133` |
| `Ubuntu.qcow2.zip` SHA-256 | `b795b6cd4c69b252c1b4f10150a347795555032501b60fd031751ed09b896712` |
| Extracted QCOW2 SHA-256 | `6bf667a852b3c307f61d9f09c42559351f45e0607e428b4997becf534cf4d313` |
| Engine source commit | `e5a1fb8e88f86b492b49ebcc426e1aa91f13301d` |
| Engine patch SHA-256 | `416ae4a818b5cdcecd18dadb254f5afaf4485a4f40e5c5f230920bececc0cd63` |
| Packaged engine build | `f1e54e1fe0b2efa90c28d0c306e93f7ce9d6e4d91ef984c4bd2347cbd24874ff` |

The image is read directly through QCOW2 and ext4 parsers, without a host mount
or VM boot. The export preserves file bytes, numeric ownership, modes, hard
links, symlinks and extended attributes. Ext4 ACLs are converted to Linux xattrs.
An independent ext4 fixture tests that conversion and inode-body slack handling.
The original image's VS Code Polish locale file has a zero xattr header followed
by nonzero inode slack; ignoring that slack preserves its original file bytes
and external attributes. Its MD5 agrees with the installed package manifest:
`828e3b9f5e52573429bfc28a8192d21a`. The QCOW2 file is not modified.

The reference's rendered desktop delta runs unchanged inside the guest. Its
GDM session, PAM setup and service files are retained. Sandweave adds its private
command agent under `/.sandweave-runtime` and uses `container=gvisor` when
starting the guest's systemd. Headless virtual consoles are enabled for this
template, inside the guest; no host terminal devices are passed through.

## Manual task completion

All three tasks were created through `Benchmark` and completed using desktop
keyboard/mouse actions. The canonical verifier ran before and after the actions.
No answer file, evaluator or task expectation was patched.

| Task | Before | After |
| --- | ---: | ---: |
| Chrome largest default font, `af630914-714e-4a24-a7bb-f9af687d3b91` | 0 | 100 |
| VS Code save workspace, `5e2d93d8-8ad0-4435-b150-1692aacaa994` | 0 | 100 |
| GNOME timezone UTC+0, `b6781586-6346-41cd-935a-a6b1487918fc` | 0 | 100 |

Screenshots show the original Ubuntu dock and application state:

![Chrome font setting](../docs/assets/osworld/chrome.png)

![Saved VS Code workspace](../docs/assets/osworld/workspace.png)

![GNOME timezone setting](../docs/assets/osworld/timezone.png)

## Runtime and concurrency checks

- Clean installed wheel: automatic setup fetched the candidate engine without
  compiling it. Commands, internet/offline networking, CPU overrides, pause/resume,
  filesystem caches and process-memory restore passed. First setup took 73.49 s;
  a subsequent coding sandbox reported 1.11 s ready time on this host.
- Installed wheel: console access permissions, user keyrings and nonseekable
  file locks passed. A live snapshot restored the active VT and independent files.
- Eight concurrent guest-service connections each transferred 256 KiB with
  request half-close. An unrelated sandbox completed 20 commands in 0.64 s.
- Twelve live benchmark tasks ran through four concurrent leases in 12.69 s.
  Each task got a distinct sandbox, an empty answer path and the expected score;
  evaluations remained in task order.
- Unit coverage includes 128 task callbacks at 64 concurrent leases,
  cancellation while setup runs, failure cleanup, evaluator shutdown draining,
  one shared fallback build for eight workers, and image receipt reuse without
  repeated full-file reads. These coordination tests use controlled fixtures.
- Engine tests passed for `auth`, `sys`, `sys_integration`, `fuse` and `config`.
- Mouse command sequences were compared against the read-only reference's actual
  `_inject` method. Keyboard source is loaded directly from that reference.

The full 50-task setup/screenshot/evaluator audit is recorded separately. Scores
from that audit describe untouched tasks, not agent performance. An initial
development run was interrupted after editing the controls changed their version
while a pool still held the previous version. The replacement audit uses a fixed,
installed wheel outside the source checkout.

## Service comparison and limits

Observed versions: Ubuntu 22.04.3, GNOME Shell 42.9, GDM 42.0, Chrome
130.0.6723.58, VS Code 1.85.1, LibreOffice 7.3.7, Xorg dummy driver 0.3.8,
Scrot 1.7 and Xdotool 3.20160805.1. The display is 1920×1080. GDM autologin,
GNOME, the session bus, SSH, OSWorld's server and the document portal run.

Five original units fail on this no-KVM engine; they are not masked or replaced:

| Unit | Observed difference |
| --- | --- |
| `avahi-daemon` | The engine lacks the address multicast groups requested through route netlink. |
| `rtkit-daemon` | Linux FIFO/RR real-time scheduling is unsupported; RealtimeKit rejects the available priority range. |
| `gpu-manager` | Physical GPU detection exits unsuccessfully in this CPU-only dummy-Xorg environment. |
| `setvtrgb` | Console palette setup cannot obtain a supported console descriptor. |
| `systemd-sysctl` | Applying the image's kernel-variable configuration fails. |

These are limits of service parity. The private reference explicitly requests
Modal's VM runtime (`vm_runtime=True`), so source equivalence alone cannot prove
its kernel behavior matches gVisor. Multicast discovery, real-time behavior and
hardware-dependent tasks require separate qualification. The 20-second boot
settle and five-second action settle are reference settings, not engine latency.

## Reproduction and retained evidence

```bash
SANDWEAVE_HOME=/path/with/enough/space sandweave doctor --check
python scripts/accept-osworld-benchmark.py --output /path/to/results
```

The script uses the installed `Benchmark` API and authenticated access to the
pinned reference; `--source` selects an existing unchanged checkout. It captures
each initial desktop and calls the canonical verifier. It does not run an agent.
Set `SANDWEAVE_OSWORLD_SOURCE` to that checkout for the input parity test and
`SANDWEAVE_OSWORLD_INTEGRATION=1` for the opt-in runtime tests.

Local raw evidence is under `/scratch/pranjala/sw-osworld-20260914`: downloaded
source images, filesystem export metadata, manual action histories, service audit,
runtime build/acceptance logs, and the `energy50-wheel` screenshots and JSONL.
Documentation screenshots and the final result summary are committed separately;
downloads, credentials, worker state and private reference source are not.

# OSWorld benchmark acceptance

Initial `0.2.20rc1` acceptance recorded on 2026-09-14. This qualifies the specified desktop and evaluator paths;
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
They were repeated on fresh sandboxes with the released engine and default
resources. No answer file, evaluator or task expectation was patched.

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

- Clean installed wheel: automatic setup fetched the published preview engine without
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
- A separate client virtual environment started with only `sandweave[benchmarks]`.
  First use installed the evaluator dependencies and started its protocol process
  in 73.11 s. The managed environment contained PyTorch `2.14.0+cpu`
  (`torch.version.cuda is None`) and OpenCV `5.0.0`. The host provided `/usr/bin/file`
  for upstream file-type checks. Reusing that environment through the committed
  `--prepare-only` script took 23.39 s, including dependency import checks.

## Representative split audit

All 50 tasks completed setup, screenshot capture and canonical evaluation through
an installed wheel outside the source checkout. This audit used one concurrent
lease, four vCPUs, 16 GiB of guest memory, 1 GiB of runtime memory and no GPU.
The separate concurrency checks above exercise pool coordination; this run does
not establish multi-worker OSWorld throughput.

| Domain | Tasks |
| --- | ---: |
| Chrome | 5 |
| GIMP | 4 |
| LibreOffice Calc | 10 |
| LibreOffice Impress | 7 |
| LibreOffice Writer | 5 |
| Multiple applications | 7 |
| GNOME settings | 1 |
| Thunderbird | 5 |
| VLC | 3 |
| VS Code | 3 |

The [aggregate record](osworld-acceptance-summary.json) verifies exact split
membership and order, 58 decoded screenshots, and the engine/resource flags and
completed cleanup for all 54 attempts. It retains the initial audit failure and
the failed follow-up replay. All recorded launch/resource/cleanup checks passed.

Most evaluations ran on untouched tasks. Their scores are not agent accuracy.
The GIMP infeasible-task check returned zero before a `FAIL` action and 100
afterward, separately from the three GUI completions above.

The initial full audit had one failed action assertion: a grouped Alt+F2,
text and Enter replay raced GNOME's Run dialog, so Settings did not open and
the timezone remained unchanged. A subsequent unattended replay remained
timing-sensitive even after splitting those actions, although the interactive
attempt passed. The final fixture includes an explicit 20-second wait after
launching Settings, captures each action's screenshot, and passes the verifier.
That wait belongs to the acceptance trajectory; the SDK's reference settle
settings are unchanged. Both failed attempts are retained.

During development, visual inspection caught background application startup
racing the first screenshot: setup had returned zero while GIMP and Calc were
still loading. Sandweave now runs the unchanged setup hook through a wrapper
that waits for matching normal, mapped application/document windows after GUI
launches, or an application-owned dialog requiring input. GIMP's color-profile
prompt is preserved. Non-GUI helpers do not incur this wait. Live regression
checks waited 3.03 s for GIMP and 9.55 s for Calc; both first screenshots
contained the requested files. The audit records window evidence alongside
screenshots and scores. Interrupted development runs are not acceptance results.

## Service comparison and limits

Observed versions: Ubuntu 22.04.3, GNOME Shell 42.9, GDM 42.0, Chrome
130.0.6723.58, VS Code 1.85.1, LibreOffice 7.3.7, Xorg dummy driver 0.3.8,
Scrot 1.7 and Xdotool 3.20160805.1. The display is 1920×1080. GDM autologin,
GNOME, the session bus, SSH, OSWorld's server and the document portal run.

Five original units failed in the initial `0.2.20rc1` acceptance below.
The [service follow-up](osworld-services.md) records the `0.2.20rc2` fixes for
Avahi, console palettes and sysctls. None of these units was masked or replaced:

| Unit | Observed difference |
| --- | --- |
| `avahi-daemon` | The engine lacks the address multicast groups requested through route netlink. |
| `rtkit-daemon` | Linux FIFO/RR real-time scheduling is unsupported; RealtimeKit rejects the available priority range. |
| `gpu-manager` | Physical GPU detection exits unsuccessfully in this CPU-only dummy-Xorg environment. |
| `setvtrgb` | Console palette setup cannot obtain a supported console descriptor. |
| `systemd-sysctl` | Writes to `vm/mmap_min_addr` and `kernel/pid_max` return `EIO`. |

These are limits of service parity. The private reference explicitly requests
Modal's VM runtime (`vm_runtime=True`), so source equivalence alone cannot prove
its kernel behavior matches gVisor. Multicast discovery, real-time behavior and
hardware-dependent tasks require separate qualification. The 20-second boot
settle and five-second action settle are reference settings, not engine latency.

## Reproduction and retained evidence

```bash
SANDWEAVE_HOME=/path/with/enough/space sandweave doctor --check
python scripts/accept-osworld-benchmark.py --output /path/to/results
# Replay the three desktop completions and the infeasible-task terminal action.
python scripts/accept-osworld-benchmark.py --output /path/to/replay-results \
    --actions tests/fixtures/osworld-actions.json
# Check automatic client evaluator preparation without starting a desktop.
python scripts/accept-osworld-benchmark.py --prepare-only --output /path/to/client-check
```

The script uses the installed `Benchmark` API and authenticated access to the
pinned reference; `--source` selects an existing unchanged checkout. It captures
each initial desktop and calls the canonical verifier. It does not run an agent.
The recorded actions are authored acceptance inputs, not copied reference code.
They use the pinned image at 1920×1080 and assert a score of zero before acting
and 100 afterward. `--task` restricts a run to a task ID, and `--cache` reuses a
previously prepared benchmark filesystem baseline.
`scripts/summarize-osworld-acceptance.py` checks the audit and any subsequent
replays against the pinned split and the tested worker's launch/cleanup records.
Set `SANDWEAVE_OSWORLD_SOURCE` to that checkout for the input parity test and
`SANDWEAVE_OSWORLD_INTEGRATION=1` for the opt-in runtime tests.

Local raw evidence is under `/scratch/pranjala/sw-osworld-20260914`: downloaded
source images, filesystem export metadata, manual action histories, service audit,
runtime build/acceptance logs, and the `energy50-qualified` and `energy50-replay`
screenshots and JSONL. Final audit evidence is also retained in the ignored
`runs/osworld-acceptance/20260914` directory of this checkout.
Documentation screenshots and the final result summary are committed separately;
downloads, credentials, worker state and private reference source are not.

A subsequent [five-task random audit](osworld-random5.md) checks fresh setups
on `0.2.20rc2`, retaining setup stdout/stderr and a visual review of each initial
desktop. The acceptance script supports `--sample` and a reproducible `--seed`.

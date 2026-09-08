# Linux Steam startup check, 2026-09-08

The standard Linux Steam bootstrap cannot execute directly in the current
gVisor sandbox: it is an i386 ELF32 executable, and the engine accepts only
ELF64. A live invocation returned `ENOEXEC` (8, "Exec format error") before
Steam started. Installing additional 32-bit libraries alone would not remove
this executable-loader restriction. No Steam sign-in screen was reached.

This is separate from Alyx's platform support. Valve published a
[native Linux version using Vulkan in May 2020](https://store.steampowered.com/news/posts/?enddate=1589576227&feed=steam_community_announcements).
The imported installation already contains the ELF64 x86-64 Linux binary
`game/bin/linuxsteamrt64/hlvr`. Our earlier Linux launch stopped at
`SteamAPI_Init` because the Linux Steam client/library was absent. The recent
menu and level-loading experiments, including the controls outside gVisor,
used the Windows game through GE-Proton Wine. They were not tests of Alyx's
native Linux game. The Windows level-loading failure and its host mapping-limit
evidence remain separate findings.

## Package and live evidence

Downloaded Valve's official
[stable Linux installer](https://repo.steampowered.com/steam/archive/stable/steam_latest.deb)
to ignored `tools/gpu/steam-client-linux-20260908/`. Package version
`1:1.0.0.87`, package architecture `amd64`, SHA256:

```text
765aba9a0ed339a50226ceb614fcc9879a991ba184098bc8de920efb12c714a4
```

Despite the package architecture, the actual bootstrap extracted from
`usr/lib/steam/bootstraplinux_ubuntu12_32.tar.xz` is
`ubuntu12_32/steam`: ELF32, Intel 80386, interpreter `/lib/ld-linux.so.2`.
Valve's [Linux client requirements](https://github.com/ValveSoftware/steam-for-linux/blob/master/README.md)
also list both 32-bit and 64-bit libraries. The local engine's
`pkg/sentry/loader/elf.go:124` rejects ELF classes other than ELFCLASS64 with
`linuxerr.ENOEXEC` (engine commit `c2f78eb`).

Created a separate sandbox `vr-steam-l40s-01` by cold-restoring
`snapshots/vr-l40s-alyx-prepared-10361186` on L40S allocation `10361186`,
node `babel-o9-20`, using explicit runtime build `d7c9c0e...`.
The original Open Saber sandbox was preserved. The package was extracted into
the shared read-only GPU asset bind, without installing packages or changing
the host. The first read failed because newly created directories inherited
restrictive permissions; granting read/traverse access only to these new
Steam staging paths resolved that preparatory error.

The successful probe read both executable headers as guest UID 1000, then used
Python `subprocess.run([steam_binary, '-version'], timeout=15)` to attempt a
direct execution, avoiding a shell fallback. It reported:

```json
{
  "steam_elf_class": 1,
  "steam_elf_machine": 3,
  "alyx_linux_elf_class": 2,
  "alyx_linux_elf_machine": 62,
  "steam_started": false,
  "exec_errno": 8
}
```

Evidence under `runs/racing/preempt-10361186/`:

- `steam-cold-restore.json`: running guest and VNC port 44279.
- `steam-linux-exec-probe.json`: complete executable headers and execution error.
- `steam-test-desktop.png`: inspected first Xvnc capture, black background and
  clipboard settings window; no Steam UI or complete desktop acceptance.
- `open-saber-after-steam-check.png`: inspected stereo Open Saber results screen,
  score 242, confirming that the original VR session remained available.

Open Saber's standalone Linux build already has verified gameplay without
Steam. AMS2 Demo instead rendered an explicit "Steam is not running" error.
SteamCMD is a separate downloader and does not establish a running authenticated
desktop Steam client. Windows Steam under Wine and Linux Steam under an
additional userspace emulator have not been tested here. No authentication
files were read or transferred, and no game authentication checks were changed.

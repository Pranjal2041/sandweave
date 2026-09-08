# Automobilista 2 Demo 2026: preparation and import

Started September 8, 2026 UTC. **Game execution and VR acceptance are pending.**

The racing game from the earlier shortlist is Automobilista 2. Its full edition
is paid; the [official Demo 2026 announcement](https://forum.reizastudios.com/threads/new-2026-automobilista-2-demo-is-out-now.36085/)
describes a free, unlimited-time demo with seven tracks and seven vehicle classes.
Steam's public app metadata identifies demo app **1786210**, depot **1786211**,
public manifest **2238040843540206676**, build **23991269**. The installed game
contains 2,371 files totaling **14,322,422,030 bytes**. The executable is
`AMS2Demo.exe`; Steam's ordinary launch adds `-novr`, its VR launch does not.
Metadata is recorded in ignored `runs/racing/steamcmd-demo-info.log`.

## Acquisition actually performed

1. Attempted official SteamCMD anonymous download on the cluster. The updated
   Linux64 client connected and retrieved app metadata, but downloading the demo
   returned **No subscription**. This path did not acquire the game.
2. Used the already logged-in Windows Steam through `ut` on `pranjala-win`.
   Opened `steam://install/1786210`, adding the free demo to its library.
3. C: lacked space. Steam's Storage UI added **D:\SteamLibrary**, which had
   43.88 GiB free. Selected D: and disabled desktop/start-menu shortcuts.
4. Installed the demo and its Steam-selected common redistributables. Steam's
   final downloads screen was opened and inspected: the demo is complete.
   The manifest reports StateFlags=4, UpdateResult=0, build 23991269, and all
   requested download/staging bytes complete. No purchase or login was needed.
5. Inventoried the completed installation and started a resumable, read-only
   transfer to the cluster. No source archive or source-file changes are needed.

No Steam authentication files were read or copied. Acquiring the installation
does not yet establish whether this demo needs an authenticated Steam client
inside the sandbox to run. No game or Steam checks have been modified.

```bash
python scripts/windows-steam-game.py status
python scripts/windows-steam-game.py inventory --output runs/racing/windows-files.json
python scripts/import-alyx.py runs/racing/windows-files.json \
  --source 'D:/SteamLibrary/steamapps/common/Automobilista 2 Demo' \
  --destination tools/gpu/racing/ams2-demo --workers 4 \
  --priority '\.(exe|dll|xml|ini)$' > runs/racing/import.log 2>&1
```

The importer retains its original Alyx filename but already accepts a source,
destination and manifest for another game. Keep each source unchanged during its
transfer. Asset storage is ignored and exposed read-only inside GPU guests.

## Sandbox prepared

`vr-racing-01` runs on the existing Slurm node, using gVisor systrap, no KVM or
host sudo: 8 advertised CPUs with weighted sharing, 32 GiB guest-page budget,
and the allocated L40S. Xvnc port is **49377**, not forwarded to Mac.
Existing Open Saber, Resolve and Alyx desktops were preserved.

```bash
python scripts/env.py start vr-racing-example --launch-options \
  --gpu 0 --guest-gs --guest-cpus 8 --memory-mib 32768 \
  --cgroup v1 --no-runtime-debug
python scripts/prepare-vr-lab.py vr-racing-example
```

Installed `libprimus-vk1 mesa-vulkan-drivers` with apt **inside this guest**.
Pinned Monado preparation completed and its service reached supported-format
initialization. The staged GE-Proton9-27 and xrizer v0.5 are shared with the
[Alyx startup experiment](alyx-startup-experiment.md). No engine changes.
Game startup, actual driving, both-eye frames, input delivery and frame rate
still need acceptance after import.

## Tracked helper tools

- `windows-steam-ui.py list|capture|click|keys` only targets visible windows
  owned by Steam/steamwebhelper. It checks foreground ownership and click bounds.
  Captures use PrintWindow with physical pixels (Windows desktop is 200% scaled).
  The generated PowerShell helper is copied to Windows TEMP and run with a
  process-local execution policy; no persistent system policy changes.
  Live acceptance: full-window captures, choosing D:, installing the demo, and
  visually inspecting the completed download. Captures are ignored in runs/racing.
- `windows-steam-game.py status|inventory` reads the selected game's manifest
  and files, with no client configuration/authentication access. Inventory
  requires the fully installed state. It emitted the actual 2,371-file manifest
  used by the active import.
- `alyx-import-status.py --pid PID` takes one foreground status sample. Optional
  `--manifest` and `--destination` select the racing import. It counts partial
  bytes as well as completed files, detects size mismatches, and checks that
  the PID is an importer. This is a size check, not a content checksum.
  Monitoring stays in the chat's sleep/check/rearm loop; the helper has no daemon.

## Isolated SteamCMD bootstrap record

`steamcmd-host.sh` wraps the **staged, updated** client in the existing Debian
Trixie Apptainer SIF. It uses its own home and staged /etc with a trusted CA
bundle; TLS verification remains enabled. It is a wrapper, not an installer.

The official [bootstrap archive](https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz)
was Linux32-only. For its initial update, extracted Debian `libc6-i386`
2.41-12+deb13u3 and `lib32gcc-s1` 14.2.0-19 into tools/steamcmd-runtime without
installing host packages. Package URLs and SHA256s are in
downloads/racing/steamcmd-runtime-packages.json. Invoking the extracted loader
through SteamCMD's DEBUGGER variable let the client update to **1788292693**.
The loader's /proc/self/exe location placed the updated files under that runtime's
usr directory; copied the resulting linux32, linux64, public, package and
steamcmd.sh into tools/steamcmd. The final wrapper selects STEAM_PLATFORM=linux64
and no longer needs the 32-bit loader.

The SIF lacks /etc/ssl. Staged its public passwd, group, hosts, resolv.conf,
nsswitch.conf and os-release under tools/steamcmd-etc, plus the host Python
installation's trusted cert.pem as ssl/certs/ca-certificates.crt. This resolved
Steam's certificate errors. Client data, dependencies, downloads, logs and local
configuration remain ignored. No host packages or authentication data changed.

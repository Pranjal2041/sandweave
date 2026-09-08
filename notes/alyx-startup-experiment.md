# Alyx: existing standalone Windows installation

Started September 8, 2026 UTC. **Startup investigation, not gameplay acceptance.**

## Result so far

The user's existing Windows executable runs under GE-Proton's Wine in gVisor,
initializes xrizer 0.5.0, creates a Vulkan OpenXR session through Monado, and loads
Alyx's action manifest and controller bindings. DXVK initializes its Direct3D 11
renderer on the allocated L40S. An experimental Primus-VK manifest change gets
past the Xvnc present-mode failure and creates a 1280x800 swapchain.

The last game probe displayed a missing/corrupted-file dialog after the targeted
resource errors had been resolved. Full import subsequently completed, and a
guest readability check found and repaired an additional directory-permission
problem, described below. A persistent [host NVIDIA driver wait](ams2-driver-stall.md)
prevents a fresh GPU run. No level, gameplay input, stereo game frames or Alyx
frame rate has been validated. Loading bindings is not acceptance of those bindings.

A separate native Linux Vulkan cube **was visibly rendered and animated** in
GNOME/Xvnc. NVIDIA renders; Primus-VK copies the image to Mesa's CPU Vulkan driver
for Xvnc presentation. Fast I/O captured two visibly different cube orientations.
This establishes a Vulkan presentation path independently of unfinished Alyx
testing; it does not establish its performance for Alyx.

The disposable sandbox is `vr-alyx-01`: 8 advertised CPUs, weighted sharing,
32 GiB guest-page budget, allocated L40S, Xvnc port 45089 (not forwarded to Mac).
Existing Open Saber and Resolve desktops were preserved. No purchase, host sudo,
KVM use or gVisor change. Engine revision remains
`59487a05f5e858d5b20a36d980036ccea2ad82ab`.

## Existing files and resumable transfer

`ut help` and `ut ls` identified `pranjala-win`. Its running Steam has a non-Steam
`hlvr` shortcut targeting the executable below. The user confirmed a standalone
copy. The library window was visually inspected; no Steam authentication files
were read. Source game files remain unchanged.

```text
C:\Program Files (x86)\Steam\steamapps\common\Half-Life Alyx\game\bin\win64\hlvr.exe
```

The installation also contains genuine ELF64 Linux binaries. The launcher and
native server module headers were checked. There are 4,539 files totaling
75,849,146,188 bytes. Windows had only about 0.11 GiB free; no archive was made
there. A small JSON inventory was written to Windows TEMP and copied with ut.
Generate equivalent records with PowerShell's `Get-ChildItem -Recurse -File`:
each record has `path` relative to the installation root, with forward slashes,
and `size` from the file's `Length`. The manifest is UTF-8 JSON without a BOM.

[import-alyx.py](../scripts/import-alyx.py) uses ut's existing `/mesh/proxy` to the
peer's `/fs/read`. Reading ut's source confirmed streaming `http.ServeContent`
with byte ranges; ordinary ut cp buffers a whole file. The importer uses bounded
8 MiB ranges, validates response ranges/lengths, resumes `.partial` files,
atomically publishes completed files, and locks against concurrent importers.
It does not hash the whole installation or detect same-size source-file edits.
Keep the source unchanged during the copy.

`--largest-first` retains explicit path priority but reverses the file-size
order within each group. After startup resources were available, the bulk
transfer was resumed with this option around 09:44 UTC at 67% complete. The
previous owned importer was stopped, preserving validated partial bytes; the
replacement immediately began the eight largest remaining maps. Independent
status samples confirmed retained bytes, new partial-file growth and no size
mismatches. The intent is to reduce the final period with too few active
streams; no measured end-to-end speedup is claimed yet. Current evidence:
`runs/alyx/import-largest-first.log`. Large-file runs can legitimately have no
file-completion log for many minutes, so use `alyx-import-status.py` to observe
partial-byte progress.

```bash
python scripts/import-alyx.py runs/alyx/windows-files.json --workers 8 \
  --priority '^game/(bin/|hlvr/bin/|core/|hlvr/shaders|hlvr/maps/startup\.vpk)' \
  > runs/alyx/import-startup-first.log 2>&1
```

Initial throughput was about 2.4 MB/s, so full copying takes hours. File counts
overstate progress because large packs hold most bytes. The current log reports
completed bytes. Files are ignored under `tools/gpu/alyx`, exposed read-only at
`/opt/engine-gpu/alyx`. Guest `/opt/alyx` holds writable directories and symlinks;
available `.cfg`, `.vcfg` and `.json` files are copied writable. This avoids
duplicating 70 GiB in the guest memory-backed writable filesystem. External
assets must remain available when reusing this experimental sandbox.

## Completed import and guest access

At **11:08 UTC**, the importer finished with exit 0. All **4,539 files** totaling
**75,849,146,188 bytes** match their manifest sizes, with no partial files left.
The final resumed phase transferred 24,745,211,773 bytes in 5,042.2 seconds;
this is a phase measurement, not the full multi-stage copy duration or a
controlled comparison of queue ordering. Foreground import monitoring ended
after observing process exit and complete files. Evidence:
`runs/alyx/import-largest-first.log` and `import-complete-status.json`.

The first guest UID 1000 check found **59 inaccessible files** behind older
directories with mode 0750. They included `game/hlvr/bin/win64/client.dll`,
`host.dll`, `server.dll`, editor modules and workshop assets. Their host files
were complete, but the ordinary guest user could not traverse their parents.
Setting umask for new copies had not repaired existing directories.

The importer now sets the manifest's parent directories to 0755 under its
destination lock, checking that resolved paths remain inside the destination.
This happens before processing cached files too. Rerunning it over the complete
installation transferred **zero bytes** and exited 0. A repeat check in
`vr-alyx-01` as UID 1000 verified every source size and read access through both
`/opt/engine-gpu/alyx` and `/opt/alyx`, with **no errors**. Writable configuration
copies were checked for read access, not equality to their original sizes.
The effect of this repair on gameplay remains untested while the driver waits.
Evidence: `runs/alyx/guest-asset-readability-before-repair.json`,
`guest-asset-readability.json` and `import-permissions-repair.log`.

Bounded controls also rejected an escaping symlink without changing its target's
mode, and verified new directory/file permissions under a restrictive inherited
default ACL. The shared filesystem does not support setting that ACL, so the
ACL control used node-local storage; resulting modes were 0755 for directories
and 0644 for the empty test file. Evidence: `runs/alyx/import-permission-controls.json`.

## Failures traced

1. Native Linux executes with Ubuntu 22.04 libraries but stops at SteamAPI_Init
   because there is no running Linux Steam client or steamclient.so. Its zero
   exit status does not indicate gameplay. Linux files alone prove insufficient.
2. GE-Proton11-6 requires GLIBC_2.38; this guest has 2.35. GE-Proton9-27 runs and
   creates its Wine prefix. No host library upgrade was made.
3. Proton's Windows VR bridge and Windows OpenVR path registry get the game to
   the bridge. Error 105 then came from missing PROTON_VR_RUNTIME: Valve's bridge
   requires that variable or its Wine registry equivalent. Pointing it at xrizer
   fixes the error and creates the actual XR session.
4. Direct Wine prefix creation omits DLLs normally staged by Proton's wrapper.
   Preparation copies bundled DXVK/vkd3d helper DLLs and vrclient_x64.dll and
   selects DXVK with Wine DLL overrides. Game and Steam checks were not patched.
5. Direct NVIDIA presentation to Xvnc fails the Vulkan present-mode query with
   VK_ERROR_UNKNOWN. Ubuntu libprimus-vk1 1.6.1-1 and mesa-vulkan-drivers
   23.2.1-1ubuntu3.1~22.04.4 enable the native cube with explicit NVIDIA vendor
   10de for rendering and Mesa vendor 10005 for presentation.
6. DXVK requests Vulkan 1.3; Primus-VK advertises 1.2. The loader disables older
   implicit layers for newer application API requests. VK_INSTANCE_LAYERS alone
   did not fix this probe. Temporarily changing the guest manifest to 1.3 loads
   the layer and permits swapchain creation. **This is a diagnostic, not Vulkan
   1.3 conformance or general layer compatibility validation.** The probe restores
   the original manifest afterward.
7. The resulting startup encounters missing VPK resources. Asset transfer must
   finish before diagnosing subsequent resource or gameplay failures.

Wine also logs unsupported seccomp/netlink operations and virtual allocation
failures. These did not prevent observed XR/renderer initialization; effects on
sustained gameplay are unmeasured. xrizer logs unsupported internal interfaces,
scroll bindings and some controller paths. All Alyx interaction remains untested.

## Reproduction

Pinned staged downloads:

- [GE-Proton9-27](https://github.com/GloriousEggroll/proton-ge-custom/releases/tag/GE-Proton9-27):
  published tarball checked against the release's .sha512sum before extraction
  to `tools/gpu/vr/GE-Proton9-27`.
- [xrizer v0.5](https://github.com/Supreeeme/xrizer/releases/tag/v0.5): zip SHA256
  `935ee21992d5cb99a2bce5b56014cc4d1f981bd9023f0eee65809b251ba7b1ee`,
  extracted to `tools/gpu/vr/xrizer-v0.5`.
- Monado and its existing patch are pinned in `scripts/prepare-vr-lab.py`.

```bash
python scripts/env.py start vr-alyx-example --launch-options \
  --gpu 0 --guest-gs --guest-cpus 8 --memory-mib 32768 \
  --cgroup v1 --no-runtime-debug
python scripts/prepare-vr-lab.py vr-alyx-example
```

Install `libprimus-vk1` and `mesa-vulkan-drivers` with apt **inside that guest**.
The original experiment installed the primus-vk metapackage, which additionally
pulled Bumblebee/Xorg into the disposable guest; those are not required here.
Use the EnvironmentManager's selected runtime command for guest execution, as
the preparation and probe scripts do. With downloads and the manifest staged:

```bash
python scripts/alyx-probe.py vr-alyx-example prepare
python scripts/alyx-probe.py vr-alyx-example cube
python scripts/alyx-probe.py vr-alyx-example native
python scripts/alyx-probe.py vr-alyx-example monado
python scripts/alyx-probe.py vr-alyx-example windows --experimental-vulkan13
```

Let Monado finish initialization before Windows: its guest journal should show
Supported formats. The monado action starts the service only; it neither launches
Open Saber nor asserts readiness. Run one probe at a time per sandbox. Probes
have guest timeouts and return their actual exit codes. Rerun prepare after import
to materialize newly available writable configuration files.

## Evidence and pending acceptance

Ignored `runs/alyx/` contains inventory, transfer/startup logs and screenshots:

- `native-initial.log`: missing Linux Steam client.
- `wine-prefix-initial.log`: successful prefix creation.
- `wine-startup.png`: visually inspected error 105 before its fix.
- `wine-game-dxvk.log`: actual XR session and NVIDIA DXVK initialization.
- `wine-game-dxvk-api13.log`: swapchain creation after the temporary override.
- `vulkan-cube-a.png`, `vulkan-cube-b.png`: inspected moving cube in Xvnc.
- `cube-script.log`, `windows-script-api13.log`: tracked probe reproduction.
- `import-startup-first.log`: ongoing runtime/core/shader-prioritized transfer.

Importer acceptance: resume a five-byte partial ELF, compare completed bytes to
the initial copy, rerun transferring zero bytes, reject a competing importer.
Native cube completed 60 frames. Pending: full assets, actual level, both composed
eyes, controller gameplay, then application submission/capture measurements.
Primus-VK with modern DXVK needs qualification beyond swapchain creation.

Primary sources: [Primus-VK](https://github.com/felixdoerre/primus_vk),
[loader API-version check](https://github.com/KhronosGroup/Vulkan-Loader/blob/v1.3.204/loader/loader.c),
[Valve VR bridge](https://github.com/ValveSoftware/Proton/blob/proton_9.0/vrclient_x64/vrclient_main.c).

## Probe at 30% imported and targeted startup archives

At 07:28 UTC the complete core directory (1,804 files), all 11 hlvr shader
archives and startup.vpk were present. Their archive files were readable as ga.
Reran preparation and the 45-second Windows probe. The earlier shader and
error-model failures disappeared. The probe timed out; the inspected Xvnc
screenshot still showed the desktop, with no game frames.

The new console log names six missing resources. `scripts/vpk-locate.py` reads
the v1/v2 VPK directory and maps selected paths to archive indices and byte
ranges, following [ValvePak's reader](https://github.com/ValveResourceFormat/ValvePak/blob/master/ValvePak/ValvePak/Package.Read.cs).
It does not extract or change game content or scan archive checksums. The six
resources map to just four missing hlvr archives:

| Archive | Resource |
|---|---|
| pak01_030.vpk | surfaceproperties/surfaceproperties.vsurf_c |
| pak01_105.vpk | Three cable_base_001 texture resources |
| pak01_174.vpk | materials/debug/particleerror.vtex_c |
| pak01_178.vpk | materials/effects/aoproxy_splat.vmat_c |

These total 431,844,251 bytes. A control located two existing core resources,
read only those 6,560 bytes and matched their individual VPK CRCs; a nonexistent
resource was correctly reported absent. Evidence: runs/alyx/vpk-locator-control.json
and missing-hlvr-at30.json. The Windows game searches hlvr before core, so an
available core version does not satisfy the missing hlvr override.

Stopped the identified importer and resumed its existing completed/partial
files with these four archives prioritized. Full copying continues afterward:

```bash
python scripts/import-alyx.py runs/alyx/windows-files.json --workers 8 \
  --priority '^game/(bin/|hlvr/bin/|core/|hlvr/shaders|hlvr/maps/startup\.vpk|hlvr/pak01_(030|105|174|178)\.vpk$)' \
  > runs/alyx/import-targeted-startup.log 2>&1
```

The probe also exposed a configuration staging gap: cfg/video.txt was a read-only
asset link. Preparation now copies every file under cfg to the guest's writable
tree, in addition to the existing configuration extensions. Verified video.txt
is a regular file owned by ga and writable by that user; source assets remain
read-only. Game configuration saving still needs verification on the next launch.
Ignored evidence includes console-at30.log, windows-at30.log, startup-at30.png,
prepare-writable-cfg.log and import-targeted-startup.log.

At 07:36 UTC all four targeted archives were complete. Set the remote headset
explicitly to (0, 1.6, 0), identity orientation, with both virtual controllers
active and optional hand-skeleton simulation disabled. The next Windows probe
no longer logged any of those six missing resources. It successfully saved
cfg/video.txt (482 bytes), establishing the writable-configuration repair.

Actual Xvnc output was a black game window with an **Unable To Start Game** dialog
reporting a missing/corrupted game file. A fresh 1920x1080 paired-eye capture was
also black. Opened and inspected both screenshots. Dismissed the dialog through
Fast I/O; the probe exited 1 with a minidump. At that point full asset import was
still needed before attributing the failure to runtime compatibility. This is not
gameplay or stereo-rendering acceptance. Evidence: runs/alyx/windows-targeted.log,
console-targeted.log, startup-targeted.png and eyes-targeted.png.

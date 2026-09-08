# Free standalone Linux VR game shortlist

Research checked 2026-09-08. Requirements: developer-supported native Linux,
actual VR on Linux, no purchase or store client/account required, and local
gameplay after downloading the executable and required content. A free demo
can qualify, but must be identified as a demo. Open source alone does not prove
that game assets are free or that the Linux build supports VR.

This is a research result. No new candidate was installed or launched in this
investigation. Open Saber is the only game below with live acceptance in this
lab. Neither a download listing nor source inspection establishes that a game
works in gVisor. A launch and gameplay test with public-network access disabled
is still needed to verify each new candidate's offline behavior.

## Strongest candidates

| Game | Reason to consider it | Linux distribution and VR path | Remaining qualification |
| --- | --- | --- | --- |
| Open Saber | Rhythm gameplay with tracked saber interaction; working baseline | Official 0.5.0 x86-64 binary, Godot/OpenXR/OpenGL | Actual gameplay already passed; separately exercise network-disabled startup |
| FlightGear | Substantial flight simulator, aircraft, scenery and cockpit procedures | Official 2024.1.7 x86-64 AppImage; upstream OpenXR through osgXR | Stage aircraft/scenery; validate this package and our graphics path; use ordinary flight controls initially |
| Clone IT | Seven VR puzzles using recorded actions and time-rewind clones | Official 75 MB Linux download; source enables OpenXR and OpenGL compatibility | Short game-jam project with documented rewind/progression bugs; no runtime acceptance here |
| Locomancer | Build model railways, handle pieces, ride trains and operate controls | Official free Linux download, with developer-documented DRM-free distribution; OpenVR/SteamVR path | Needs an xrizer/Monado compatibility test; no direct OpenXR claim |
| GunSpinning VR | Western rail shooter with training, story and score modes; eight advertised levels | Official free Linux 2.0.1 download; developer explicitly lists Linux SteamVR controller support | Older Linux release; OpenVR bridge and offline startup untested |

The ordering reflects technical fit and variety, not an experimentally measured
ranking of quality or speed. These are independent games and simulations; this
search did not establish a free, native Linux, store-independent equivalent to
Alyx's production scale and campaign.

### Open Saber

The [developer's page](https://leandrodreamer.itch.io/open-saber) provides a free
Linux x86-64 build and identifies OpenXR support. One song is bundled; acquiring
extra songs is separate from playing already installed content. See
[our gameplay acceptance](vr-monado-experiment.md) and
[fresh GPU results](preempt-vr-debug.md). The prior acceptance did not explicitly
disable network access, so it is not a controlled offline test.

### FlightGear

The [official download](https://www.flightgear.org/download/) currently provides
2024.1.7 for Linux x86-64, approximately 329 MB for the AppImage alone.
Aircraft, base data and scenery are additional staging considerations.
[Upstream VR documentation](https://wiki.flightgear.org/Virtual_Reality)
uses OpenXR, supports enabling VR with `--enable-vr`, and describes Linux
packaging. The [development status](https://wiki.flightgear.org/Virtual_reality/Development)
reports working VR in release/2024.1 and Linux CI builds, with Monado test
configurations. Tracked-controller flight controls are described in a separate
stale development branch, so do not promise hand-operated flight controls in the
stable build. Keyboard, mouse or joystick control is the initial scope.

Offline operation requires local aircraft/scenery and avoiding online services
such as real-weather retrieval, multiplayer and automatic scenery downloads.
[TerraSync](https://wiki.flightgear.org/TerraSync) is the optional scenery-fetch
mechanism. This simulator is the most substantial new content candidate with
an upstream OpenXR path, but that does not establish sandbox compatibility or
high frame rates.

### Clone IT

The [authors' page](https://fyoxy.itch.io/clone-it) publishes
`CLONE_IT_FINAL_Linux.zip`, with instructions to extract and run. Its gameplay
records the player's actions and replays them as clones to solve seven puzzles.
The authors disclose a bracelet-spamming bug that can stop rewind/progression;
spacebar rewind or restarting is their workaround.

Read the authors' source at commit
`0061d6808ae355a23f50b012f30b770c21e9c953`:
[project.godot](https://github.com/Fyoxy/clone-it/blob/0061d6808ae355a23f50b012f30b770c21e9c953/project.godot)
sets Godot 4.6, `openxr/enabled=true`, and
`renderer/rendering_method="gl_compatibility"`;
[export_presets.cfg](https://github.com/Fyoxy/clone-it/blob/0061d6808ae355a23f50b012f30b770c21e9c953/export_presets.cfg)
contains a Linux/X11 x86-64 export. This is a close architectural match to
Open Saber's tested path. It is a compatibility inference, not a verified
match between the published binary and that source revision or a gameplay test.

### Locomancer

The [developer's page](https://selkcip.itch.io/locomancer) offers
`Locomancer_Linux_2025_01_16.zip` (387 MB) as a free download. The author also
published a [DRM-free build announcement](https://selkcip.itch.io/locomancer/devlog/151706/new-drm-free-builds)
with a Linux archive. Its appeal here is extensive direct interaction:
placing tracks/scenery, riding the layout and operating train controls.
The page describes a SteamVR headset/runtime interface; it does not establish
compatibility with our Monado environment.

### GunSpinning VR

The [developer's page](https://demonixis.itch.io/gunspinning-vr) lists a free
397 MB Linux 2.0.1 archive, eight levels, and SteamVR controller support on
Linux. That is stronger Linux-VR evidence than generic platform tags. Its
story/shooting loop is useful variety beyond the rhythm game, but the Linux
release trails the Quest release. No network-disabled or bridge acceptance
has been performed here.

## OpenVR is an extra compatibility step

[xrizer's upstream documentation](https://github.com/Supreeeme/xrizer) says it
implements OpenVR on OpenXR without SteamVR, including configuration when
SteamVR has never been installed. This is relevant to the standalone builds
of Locomancer and GunSpinning VR. It does not remove a game's Steam account
requirement, where one exists, and it does not guarantee every OpenVR interface
or game's behavior. Our earlier Windows game-menu acceptance is not acceptance
of these Linux binaries.

Native OpenXR games are consequently the closest match to our existing setup;
OpenVR titles are promising additional candidates, with the bridge explicitly
part of their unverified work.

## Conditional and rejected matches

| Title | Finding |
| --- | --- |
| Traks demo | The [developer's VR/Linux update](https://ga-games.itch.io/traks/devlog/1417759/vr-support-and-other-good-stuff) announces both VR and a native Linux build, but explicitly says the author cannot test Linux. The [current page](https://ga-games.itch.io/traks) still serves the older 570 MB Linux demo alongside newer Windows builds. Attractive car-racing candidate, not established as easy or tested Linux VR. |
| Project SEVER demo | The [author publishes](https://mehozavr.itch.io/sever) an older Linux 0.4.2 download and an atmospheric submarine adventure. Current explicit VR platform text emphasizes Windows/SteamVR; the page does not establish that the offered Linux build's VR path works with Monado. Keep conditional, not a proven Linux VR recommendation. |
| HyperRogue | [Upstream source](https://github.com/zenorogue/hyperrogue) supports Linux builds and a VR build option; the [VR announcement](https://zenorogue.itch.io/hyperrogue/devlog/260131/hyperrogue-120-dice-curses-and-vr) identifies SteamVR. Free source is available, but the current standalone download page does not supply a Linux binary. More build/integration work than the shortlist. |
| Astral Slider | The [author provides](https://kylerodgers.itch.io/astral-slider) a free Linux build and advertises PCVR, but the page does not establish the Linux build's XR backend. Needs package inspection before promotion. |
| Fly Dangerous | Despite an official Linux download, the [developer's README](https://github.com/jukibom/FlyDangerous/blob/main/README.md) says VR uses Unity OpenXR on Windows and directs Linux VR users to Proton. Fails this task's native Linux VR criterion. |
| Galaxy Forces VR | The [developer explicitly says](https://rh-galaxy.itch.io/galaxy-forces-vr) the standalone desktop builds require a different store download for VR. Linux plus a VR tag does not qualify this archive. |
| Starbase Simulator | The [developer distinguishes](https://ashtorak.itch.io/starbase-simulator) current Unreal Linux builds from old Unity builds; simple VR exists in the latter and is absent from the former. Not a verified current Linux VR match. |
| Neverball | [Upstream build instructions](https://github.com/Neverball/neverball/blob/master/doc/install.txt) expose OpenHMD or legacy Oculus integration, not our OpenXR path. Good native Linux game, additional VR backend integration required. |

## Proposed acceptance scope

For the closest new implementation match, test Clone IT in a separate Xvnc
GPU sandbox. For a substantial simulator, test FlightGear with one local
aircraft/scenery area. For richer hands-on object interaction, qualify
Locomancer's standalone Linux build through xrizer. This is a proposed order,
not work already executed.

For each: verify an ELF64 Linux executable, launch without a store client,
capture both eyes, exercise the relevant controls into actual gameplay, and
repeat cold startup with external networking disabled. Measure application
frame submissions separately from compositor target Hz. Keep user-facing
desktops and existing assets intact; no new GPU jobs or runtime changes were
made for this research.

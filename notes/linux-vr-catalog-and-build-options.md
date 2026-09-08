# Linux VR games and build options for the no-KVM lab

Research checked **2026-09-08**. Windows VM development is paused at the user's
request. The objective remains capable, fast sandboxes without host sudo or
KVM, with VR games as the current workload. This catalogue expands the earlier
[free standalone shortlist](standalone-linux-vr-games.md).

The user's second category is interpreted as Windows games brought to Linux.
Running their Windows executables through Wine/Proton does not require a Windows
VM. A native source port, a compatibility-layer run, and adding VR to a flat game
are different engineering tasks.

This is research, not a new runtime acceptance report. No games were installed
or launched, no jobs submitted, and no desktops or runtime settings changed.
Open Saber remains the only game with accepted gameplay in this lab. Alyx reached
menus but failed level loading; the Automobilista 2 demo reached a Steam error.
See [local evidence](preempt-vr-debug.md) and [Open Saber acceptance](vr-monado-experiment.md).

## What the available evidence establishes

- **Local acceptance:** gameplay in our patched gVisor environment.
- **Upstream Linux VR:** the game/port author supplies a Linux VR build or explicit
  Linux VR source/build instructions. It still needs testing here.
- **Reported Linux gameplay:** a named tester describes VR gameplay on a specified
  Linux configuration. This is evidence of an existing route, not certification
  of current releases, all controllers, NVIDIA, gVisor, or offline operation.
- **Candidate:** platform metadata, source mechanisms or an existing VR port on a
  different platform provide a reason to investigate. Linux VR success is not
  established by this label.
- **Proposal:** original engineering work we could undertake; no implementation
  or performance result is implied.

The catalogue is broad, not exhaustive. A Linux platform icon plus a VR tag is
insufficient: those can describe different executable builds. A successful
SteamVR report does not prove Monado/xrizer compatibility. Most detailed external
gameplay reports inspected here date from 2025 through January 2026.

The earlier preference for free, local gameplay without Steam/accounts remains
the strongest deployment fit. Paid/store titles appear separately to show the
wider possibilities; inclusion does not authorize purchases or imply that those
conditions have been relaxed. Free downloads and permission to redistribute
game assets are also separate questions.

## Native Linux VR supplied by the game developer

| Game | Useful interaction/content | Distribution and Linux evidence | Qualification for this lab |
|---|---|---|---|
| **Open Saber** | Two-handed rhythm, timing, dodging | Free standalone Linux; Godot/OpenXR; [developer](https://leandrodreamer.itch.io/open-saber), [source](https://github.com/leandrodreamer/BeepSaber/tree/godot-4-port) | **Gameplay accepted here.** Sampled 90 FPS without continuous desktop mirror, 61 FPS with mirror; not rollout throughput. One bundled song; additional music is separate content. |
| **Clone IT** | Seven puzzles with recorded actions and time-rewind clones | Free Linux download, Godot/OpenXR; [developer](https://fyoxy.itch.io/clone-it), [MIT source](https://github.com/Fyoxy/clone-it) | Closest new match to our tested engine path. Jam scope, disclosed rewind/progression bugs; not tested here. |
| **FlightGear** | Aircraft, navigation, instruments, procedures, scenery | Free native Linux; OpenXR via osgXR; [VR documentation](https://wiki.flightgear.org/Virtual_Reality), [development status](https://wiki.flightgear.org/Virtual_reality/Development) | Substantial simulation. Stage aircraft/scenery for offline use. Begin with keyboard/joystick controls; stable hand-operated cockpit coverage is not established. |
| **Locomancer** | Build model railways, manipulate pieces, ride and operate trains | Free standalone Linux, developer's DRM-free builds; [download](https://selkcip.itch.io/locomancer), [distribution announcement](https://selkcip.itch.io/locomancer/devlog/151706/new-drm-free-builds) | Rich hand interaction. OpenVR bridge to Monado needs qualification; free executable does not establish source/redistribution rights. |
| **GunSpinning VR** | Western shooting, story/training/score modes | Free standalone Linux 2.0.1; author explicitly lists Linux SteamVR controllers; [developer](https://demonixis.itch.io/gunspinning-vr) | Older Linux build; test OpenVR bridge and offline launch. |
| **HyperRogue** | Procedural non-Euclidean navigation and tactical puzzles | Free GPL source with Linux builds and `-vr` build option; [upstream](https://github.com/zenorogue/hyperrogue), [VR announcement](https://zenorogue.itch.io/hyperrogue/devlog/260131/hyperrogue-120-dice-curses-and-vr) | Source build/integration needed for a standalone package. Different interaction model from a hands-on physics adventure. |
| **The Talos Principle VR** | Full spatial puzzle campaign, object placement, planning | Paid Steam release with explicit Linux headset/controller requirements; [store](https://store.steampowered.com/app/552440/) | Publisher says initial online activation, then persistent internet unnecessary. Store/client issue remains separate from OpenVR bridging. |
| **Serious Sam VR: The First Encounter** | Full action campaign, aiming and navigation | Paid Steam, Linux VR requirements; [store](https://store.steampowered.com/app/552450/) | Publisher specifies initial activation, offline play thereafter. No local gameplay test. |
| **Serious Sam VR: The Second Encounter** | Another substantial action campaign | Paid Steam, Linux VR requirements; [store](https://store.steampowered.com/app/552460/) | Same activation/store/bridge qualifications. |
| **Serious Sam 3 VR: BFE** | Larger shooter environments and encounters | Paid Steam, Linux VR requirements; [store](https://store.steampowered.com/app/567670/) | Same activation/store/bridge qualifications. Do not extend this claim to every Serious Sam VR title. |
| **Groove Gunner** | Rhythm with shooting and blocking | Paid VR-only game with Linux system requirements; [store](https://store.steampowered.com/app/976930/) | Upstream Linux target; no local or detailed current Monado acceptance established in this survey. |
| **Fake Racing** | Arcade racing, including steering with tracked hands | Paid Linux build; Linux requirements explicitly mention VR; [developer's listing](https://store.steampowered.com/app/1481600/Fake_Racing/) | A relevant native racing candidate; no sandbox acceptance. |

**Half-Life: Alyx is a special case.** The lab has already inspected its actual
native Linux executable, so its existence is established. However, the current
[Steam metadata](https://store.steampowered.com/api/appdetails?appids=546560&cc=us&l=english)
reports `linux: false` and empty Linux requirements. Therefore this catalogue
does not assert current official Linux support solely from its historical
availability. Our native launch stopped at Steam initialization; Windows/Wine
menus worked but level gameplay did not. Neither result is a recommendation to
resume Alyx debugging during this research task.

## Community VR ports that can execute natively on Linux

These can reuse large games without a Windows executable, but support comes from
the port maintainers rather than the original commercial publisher.

| Game/port | What already exists | Cost/store situation | Main limitation |
|---|---|---|---|
| **Minecraft Java + Vivecraft** | Linux OpenVR support; roomscale and tracked-controller gameplay. [Project FAQ](https://www.vivecraft.org/faq/), [current source](https://github.com/Vivecraft/VivecraftMod) | Mod free; Minecraft Java ownership/account needed. No Steam purchase needed. | Existing FAQ's OpenComposite warning is old; xrizer 0.4 names Vivecraft among newly supported games. Account/bootstrap and cached offline startup need qualification. |
| **Morrowind + OpenMW-VR** | OpenXR VR engine fork; explicit Linux build instructions. [Source](https://gitlab.com/madsbuvi/openmw/-/tree/openmw-vr), [Linux installation](https://openmw-vr.readthedocs.io/en/latest/manuals/installation/install-openmw-vr.html) | Engine free; requires owned Morrowind assets. Can use assets acquired without a Steam runtime. | Separate fork, source build; maintainer explicitly relies on community Linux fixes beyond CI compilation. Large RPG content is attractive, but no current sandbox test. |
| **The Dark Mod VR** | Free stealth game, Linux builds and OpenXR. [VR repository](https://github.com/fholger/thedarkmodvr), [releases](https://github.com/fholger/thedarkmodvr/releases) | No paid base game required; local missions possible. | VR work is on hold; last inspected release targets base 2.10. Seated keyboard/mouse/gamepad, **no tracked-hand or roomscale gameplay**. Match missions to that version. |
| **Minetest VR / Luanti lineage** | Linux OpenXR alpha, voxel exploration/building. [Port author's status](https://dustlabs.io/minetestvr.html) | Free engine; select a compatible free game/content package. No Minecraft account required. | Seated keyboard/mouse only; hands and roomscale are future work. Older fork is not proof of compatibility with current Luanti or every game/mod. |

The Dark Mod's source and assets need separate treatment: its
[licence file](https://github.com/fholger/thedarkmodvr/blob/master/LICENSE.txt)
identifies noncommercial/share-alike terms for most assets and distinct terms
for downloaded missions. Thus it is a useful free-play/content precedent, but
not automatically an unrestricted redistributable training package.

## Native listings needing more qualification

| Title | What is established | Why it is not promoted to the stronger list |
|---|---|---|
| **Balloonatics** | Publisher has Linux builds, VR support, single-player and multiplayer; [store](https://store.steampowered.com/app/744600/) | Need Linux VR runtime evidence and actual local/single-player scope, not just tags. |
| **Polynomial 2** | Publisher Linux build and VR-advertised space/music shooter; [store](https://store.steampowered.com/app/379420/) | Exact current Linux VR path and bridge behavior unqualified. |
| **PULSAR: Lost Colony** | Linux build and cooperative starship game with VR; [store](https://store.steampowered.com/app/252870/) | Linux VR combination, executable architecture, and offline bot-crew play need qualification. |
| **Zaccaria Pinball** | Linux platform and VR-advertised pinball; [store](https://store.steampowered.com/app/444930/) | Free base/trials do not mean all tables or VR access are free. Verify exact product and Linux VR backend. |
| **BeamNG.drive** | Experimental native Linux and VR code paths; [developer discussion](https://www.beamng.com/threads/experimental-virtual-reality.94206/page-27), [current known issues](https://www.docs.beamng.com/known-issues/) | Experimental Vulkan/Linux/VR combination with driver and rendering issues. Do not label it stable native VR. |
| **X-Plane 12** | Native Linux simulator and substantial VR development; [12.4.3 release notes](https://www.x-plane.com/kb/x-plane-12-4-3-release-notes/) | Official Linux VR support not established. An older [developer statement](https://developer.x-plane.com/2020/04/x-plane-11-50-public-beta-1-vulkan-and-metal-are-here/) distinguished working from officially supported Linux VR. New general VR features alone do not settle this. |
| **Traks demo** | Developer publishes Linux and announces VR; [announcement](https://ga-games.itch.io/traks/devlog/1417759/vr-support-and-other-good-stuff) | Author explicitly cannot test Linux; demo/build version differences. |
| **Project SEVER demo / Astral Slider** | Linux downloads and VR advertising; [SEVER](https://mehozavr.itch.io/sever), [Astral Slider](https://kylerodgers.itch.io/astral-slider) | Exact Linux VR build/backend evidence remains insufficient; retain earlier shortlist qualifications. |

Important corrections to broad online lists:

- **Distance:** a Linux flat build exists, but the inspected VR tester explicitly
  used Proton because VR was absent from the native version. Put its known route
  in the Windows compatibility group. [Firsthand report](https://db.vronlinux.org/games/233610.html).
- **BallisticNG:** native Linux plus VR tags are insufficient. A July 2025 tester
  explicitly reports native VR failure and successful Proton VR; other reports
  do not reliably identify native versus Proton. Retain that ambiguity.
  [Firsthand reports](https://db.vronlinux.org/games/473770.html).
- **Fly Dangerous:** the developer explicitly directs Linux VR users to Proton;
  Linux store requirements say VR unsupported. [Developer README](https://github.com/jukibom/FlyDangerous).
- **Open Brush:** current Linux archives exist, including 2.32.0, but the current
  [store requirements](https://store.steampowered.com/app/1634870/Open_Brush/)
  explicitly restrict Linux to viewer/monoscopic modes. Earlier Linux VR work
  and a current Linux ZIP do not establish current Linux VR support.
- **Serious Sam VR: The Last Hope** is not included with the other three Sam
  titles: current [store metadata](https://store.steampowered.com/api/appdetails?appids=465240&cc=us&l=english)
  reports no Linux platform. **Munch VR** and **Universe Sandbox** also have
  conflicting historical/current platform metadata and are not verified current
  Linux VR entries here.

## Windows VR games with documented Linux routes

Wine/Proton translates the Windows application interfaces; graphics and XR
compatibility are additional components. No Windows VM or KVM is intrinsic to
this route. [xrizer](https://github.com/Supreeeme/xrizer) can replace SteamVR for
OpenVR calls, but cannot remove a game's Steam-client or ownership requirement.

The following are **reported gameplay elsewhere**, not our acceptance list.
Unless stated otherwise these are paid commercial titles, with the inspected
reports using Steam/Proton. Detailed reports were read, including contrary
reports; aggregate ratings were not treated as proof. Each link points to the
firsthand records, which contain versions/hardware/dates.

| Game | Training-relevant gameplay | Evidence and caveat |
|---|---|---|
| **Beat Saber** | Rhythm and coordinated hands | Multiple successful Linux reports, including NVIDIA; later overlay and version regressions, some pinning 1.40.8. [Reports](https://db.vronlinux.org/games/620980.html). |
| **Pistol Whip** | Aiming, dodging and rhythm | Multiple successful Monado/WiVRn reports; April 2025 records Proton 9.0-4 plus OpenComposite. [Reports](https://db.vronlinux.org/games/1079800.html). |
| **SUPERHOT VR** | Motion-dependent time, aiming, throwing, evasion | Successful SteamVR and Monado/OpenComposite gameplay, including NVIDIA. [Reports](https://db.vronlinux.org/games/617830.html). |
| **VTOL VR** | Cockpit switches, procedures and flying | Detailed report includes an hour in missions; SteamVR and WiVRn successes. [Reports](https://db.vronlinux.org/games/667970.html). |
| **Skyrim VR** | Long-horizon exploration, quests and combat | Multiple successes; mod setup and naming/keyboard UI depend on bridge/version. [Reports](https://db.vronlinux.org/games/611670.html). |
| **Fallout 4 VR** | Exploration, scavenging and combat | Only one inspected DB report, specifying Monado/OpenComposite/Proton 9.0-4; thinner evidence. [Report](https://db.vronlinux.org/games/611660.html). |
| **Into the Radius** | Inventory, survival and deliberate tool/weapon handling | Successes with SteamVR and OpenComposite/WiVRn; also poor NVIDIA performance reports. [Reports](https://db.vronlinux.org/games/1012790.html). |
| **BONEWORKS** | Physics interaction and traversal | Successful gameplay, but controller and stuck-overlay regressions vary by route. [Reports](https://db.vronlinux.org/games/823500.html). |
| **BONELAB** | Physics tasks and modded scenarios | Late-2025/January-2026 successful reports; OpenXR discovery and controller offsets can require setup. [Reports](https://db.vronlinux.org/games/1592190.html). |
| **Blade and Sorcery** | Melee, grabbing and physics | Working gameplay documented, but spells, scrolling and sticky inputs recur with alternative runtimes. [Reports](https://db.vronlinux.org/games/629730.html). |
| **Job Simulator** | Object use and multistep work tasks | One tester reports OpenComposite/xrizer success; another reports controller offsets/bindings. [Reports](https://db.vronlinux.org/games/448280.html). |
| **Walkabout Mini Golf** | Precision club-ball contact | October 2025 report confirms putting after an xrizer fix; earlier failures remain in history. [Reports](https://db.vronlinux.org/games/1408230.html). |
| **Until You Fall** | Timing, blocking and combat | Multiple xrizer successes; OpenComposite startup failures and some later crashes. [Reports](https://db.vronlinux.org/games/858260.html). |
| **Moss** | Third-person diorama puzzles and direct interaction | Successful SteamVR and particular xrizer configurations; binding files/controllers cause failures elsewhere. [Reports](https://db.vronlinux.org/games/846470.html). |
| **I Expect You To Die 2** | Seated escape puzzles and tool use | Successful configured Monado/SteamVR reports; later NVIDIA/WiVRn report has severe sticky inputs. [Reports](https://db.vronlinux.org/games/1499120.html). |
| **Space Pirate Trainer** | Aiming, defense and dodging | Two configuration-specific successful reports. [Reports](https://db.vronlinux.org/games/418650.html). |
| **Tetris Effect: Connected** | Planning and audiovisual attention | Successful Linux VR reports; game remains a board task rather than hand manipulation. [Reports](https://db.vronlinux.org/games/1003590.html). |
| **No Man's Sky** | Procedural exploration, crafting and vehicles | Successful xrizer gameplay as well as startup/performance failures. [Reports](https://db.vronlinux.org/games/275850.html). A [paid GOG distribution](https://www.gog.com/en/game/no_mans_sky) exists, but the inspected Steam reports do not verify that package in our offline sandbox. |
| **Distance** | Fast racing and platforming | Explicit Windows/Proton VR success, including OpenComposite/xrizer; menu orientation/world scale can need adjustment. [Reports](https://db.vronlinux.org/games/233610.html). |
| **BallisticNG** | Antigravity racing | Explicit Proton 9.0-4 VR success following native failure. [Reports](https://db.vronlinux.org/games/473770.html). |
| **Half-Life 2: VR Mod** | Full adventure with VR interactions | Actual Linux gameplay reports and upstream xrizer improvements; legacy **32-bit** components add a specific issue for our current loader. [Reports](https://db.vronlinux.org/games/658920.html), [xrizer 0.4](https://github.com/Supreeeme/xrizer/releases/tag/v0.4). Do not infer Episode One/Two acceptance without separate checks. |

Additional routes with narrower evidence or existing blockers:

- **Fly Dangerous:** especially relevant because its author supplies a
  [free non-Steam download](https://jukibom.itch.io/fly-dangerous) and explicitly
  identifies Proton as the Linux VR route. This is upstream route documentation,
  not a successful test here; the inspected LVRA entry had zero reports. OpenXR
  is headset-only; steering uses gamepad/joystick/keyboard. Source includes UDP
  telemetry, but a complete source build has asset dependencies and the itch
  page lists noncommercial asset terms. [Source/build details](https://github.com/jukibom/FlyDangerous).
- **Derail Valley:** rich train operations; xrizer 0.4 explicitly names it among
  games that should now work. That upstream statement is weaker than a current
  full gameplay test. [Release](https://github.com/Supreeeme/xrizer/releases/tag/v0.4).
- **Euro Truck Simulator 2 / American Truck Simulator:** published Linux VR setup
  uses the Windows build through Proton; do not mistake their native flat builds
  for proof of native VR. [Route documentation](https://vronlinux.org/docs/games/ets2-ats/).
- **Automobilista 2:** external Linux reports exist, but our demo still stops at
  Steam sign-in, and no race is accepted here. [External reports](https://db.vronlinux.org/games/1066890.html), [local evidence](ams2-demo-experiment.md).

The current lab also rejects Linux Steam's 32-bit bootstrap. Working on a normal
Linux desktop is therefore a separate milestone from working in this sandbox.
Wine's newer WoW64 execution can be relevant to some 32-bit Windows programs,
but that does not automatically supply 32-bit native Linux XR libraries or fix
the Steam client. [Local Steam investigation](steam-client-experiment.md).

## Existing flat games and VR ports we could develop further

Effort assessments below are engineering judgments for a focused, tested
version, not delivery promises. They include camera/rendering integration,
input/UI and representative gameplay. Adding convincing hands, climbing,
physics interactions and complete campaign compatibility is additional work.

| Base or family | Existing reusable work | Proposed Linux VR work and relative scope |
|---|---|---|
| **Luanti + a compatible free voxel game** | Native source, moddable worlds, existing seated OpenXR alpha; [Luanti](https://github.com/luanti-org/luanti), [VR fork](https://dustlabs.io/minetestvr.html) | Add tracked hands, world-space inventory, block interaction and reset/task API. Medium-to-large; choose/pin a compatible game before assuming mod coverage. |
| **SuperTuxKart** | Complete native Linux open-source racer with tracks/opponents; [source](https://github.com/supertuxkart/stk-code) | Stereo OpenXR rendering, cockpit/chase view, VR menus, optional hand steering. Medium-to-large engine integration; no existing accepted VR build claimed. |
| **Stunt Rally 3** | Native source, driving physics, tracks and editor; [source](https://github.com/stuntrally/stuntrally3) | OpenXR camera/rendering and cockpit UI. Reuses much more content/physics than writing a simulator from scratch, but remains a significant renderer port. |
| **Neverball / Neverputt** | Native small games; actual legacy OpenHMD/Oculus integration in [upstream build options](https://github.com/Neverball/neverball/blob/master/doc/install.txt) | Modernize the backend to OpenXR and add hand/table controls. Relatively bounded, with input design still required; legacy HMD support is not Monado acceptance. |
| **Quake / LibreQuake + Quake VR** | Existing roomscale, grabbing and weapon VR implementation; [Quake VR](https://github.com/vittorioromeo/quakevr). [LibreQuake](https://github.com/lavenderdotpet/LibreQuake) supplies free replacement content | Port/maintain the desktop Linux VR backend; verify game-code and asset compatibility. Medium-to-large. The inspected Quake VR distribution is not a verified native Linux client. |
| **Freedoom + a Doom VR engine** | Free replacement game content and established VR engine forks; [Freedoom](https://freedoom.github.io/about.html), [GZDoomVR](https://github.com/hh79/gzdoomvr) | Qualify or port the particular Linux XR backend, UI and hands. Good way to avoid proprietary Doom assets; not an already tested package here. |
| **Doom 3 / Doom 3 BFG** | Actual VR adaptations in [Doom3Quest](https://github.com/Team-Beef-Studios/Doom3Quest), [Fully Possessed](https://github.com/KozGit/DOOM-3-BFG-VR), older [Linux/OpenVR fork](https://github.com/Codes4Fun/RBDOOM-3-BFG) | Reconcile exact engine/renderer/platform versions. Original versus BFG assets differ. Large integration and qualification task, with paid game data. |
| **Jedi Outcast / Jedi Academy** | [JKXR](https://github.com/Team-Beef-Studios/JKXR) already implements OpenXR motion-controlled lightsabers and other VR interactions on supported platforms | Build on the OpenJK lineage and port/qualify desktop Linux. Existing Quest/PCVR support does not prove Linux support. Paid original assets. |
| **Half-Life 1** | [Lambda1VR](https://github.com/Team-Beef-Studios/Lambda1VR) adds VR to Xash3D on Quest | Port Android-specific XR/platform code to desktop Linux and qualify physics/input. An APK is not a Linux desktop executable. Original game assets needed. |
| **Open Brush** | Extensive open-source 3D painting tools; [source](https://github.com/icosa-foundation/open-brush) | Restore a supported Linux VR backend or port selected interaction concepts. Current Linux package is desktop-only. Useful creation tasks, though not a scored game by itself. |

Two broader mechanisms are real but should not inflate the verified list:

- **UEVR + Kaon:** [UEVR](https://github.com/praydog/UEVR) injects VR into many
  Unreal games; [Kaon](https://github.com/LorenDB/kaon) manages it on Linux through
  Proton and explicitly describes itself as pre-alpha. This is an extra
  compatibility/modding route, not a native Linux export or proof that every
  Unreal game works. Hand interactions and game profiles remain game-specific.
- **DolphinXR:** a recent [fork](https://github.com/iChris4/dolphinXR) explicitly
  documents desktop Linux OpenXR builds. It offers a way to investigate VR views
  of GameCube/Wii games. This survey inspected its documentation, not a runtime
  or title-specific result; mature compatibility, motion interaction and game
  content availability are separate questions. It is outside the native-game
  core proposed here.

For a **3D game played on a monitor**, much of the world and logic already exists;
we adapt cameras, rendering, scale, UI and controls. For a genuinely **2D game**,
a VR version normally needs a design choice: preserve it on a virtual board or
diorama, or rebuild its mechanics as a 3D environment. It is not a general export
switch. A diorama may be excellent for planning, but adds fewer embodiment
demands than physically manipulating tools in first person.

## Original VR games we could build

These are original games inspired by interaction patterns, with original or
appropriately licensed content. They are not promises to reproduce the named
commercial games' campaigns, art, music, characters or production polish.

| Interaction family / reference | Concrete original game | Principal training behavior | Relative scope |
|---|---|---|---|
| Beat Saber / Pistol Whip | Seeded rhythm arenas with procedural targets and obstacles | Bimanual timing, prediction and evasion | Small-to-medium; existing Open Saber provides a tested starting point |
| Walkabout Mini Golf | Procedurally assembled mini-golf courses | Precision contact, trajectory and planning | Small-to-medium; high-quality contact needs tuning |
| Eleven Table Tennis | Table tennis with adjustable opponents and ball machines | Interception and rapid feedback control | Medium; credible spin/contact and high-speed collision are core work |
| Space Pirate Trainer | Target defense with shields, projectiles and cover | Aim, dodge, block and prioritize | Small-to-medium |
| The Room / I Expect You To Die | Escape rooms with locks, circuits, drawers and tools | Long sequences, search, memory and object use | Medium; reusable puzzle components provide content variation |
| Job Simulator / cooking games | Workshop or kitchen with assembly, sorting and recipes | Bimanual manipulation and tool use | Medium for rigid-object tasks; realistic liquids/cutting increase scope |
| SUPERHOT VR | Arenas where simulation speed follows player movement | Planning under motion/time constraints | Medium; coherent timing across physics, AI and XR needs design |
| Climbing / grappling games | Traversal courses with handholds, ropes and moving platforms | Reach planning and coordinated locomotion | Medium; stable body/hand physics is substantial work |
| Locomancer / construction toys | Rail or mechanical assembly with snap joints, switches and vehicles | Assembly, spatial planning and control | Medium, with bounded component libraries |
| Cockpit / kart games | Short flight, racing or rover courses with interactive controls | Navigation, continuous control and procedures | Medium arcade model; realistic simulation is much larger |
| Minecraft / Luanti | Voxel gathering, crafting and building scenarios | Long-horizon resource use and construction | Medium-to-large; reuse a voxel engine rather than rebuild everything |
| Alyx / BONEWORKS | Compact physics adventure with doors, tools, inventory and enemies | Integrated exploration and manipulation | Large even for a vertical slice; full production-scale campaign far larger |

I have high confidence in the feasibility of the smaller original-game scopes
using existing engines. That confidence does not establish a schedule, target
frame rate or physical realism. More coding agents can divide independent
modules, content tools and test cases; integration, physics correctness and
game quality still need shared specifications and actual playtesting.

## A practical choice for the training platform

My recommendation is a **native Linux/OpenXR core**, with source-controlled
games and original tasks, plus a separately qualified catalogue of commercial
and community ports. This preserves broad sandbox scope while giving training
users a dependable initial workload set.

**Godot 4 + OpenXR + Godot XR Tools** has the closest local evidence: Open Saber
already runs through that family of components in our sandbox.
[XR Tools](https://github.com/GodotVR/godot-xr-tools) supplies reusable interaction
and locomotion components. [Godot RL Agents](https://github.com/edbeeching/godot_rl_agents)
provides an existing Python integration for training 2D/3D Godot agents; it is
not by itself an implemented VR-head/hands adapter or deterministic simulation
clock. [LÖVR](https://lovr.org/) is another native Linux/OpenXR option for compact
code-driven environments, with Vulkan as an additional graphics qualification.

The architecture keeps the roles separate: gVisor isolates the Linux process;
Monado provides XR; Xvnc remains the default desktop/control display. Neither a
physical headset nor SteamVR is intrinsically required for our virtual-device
training route. Real-headset streaming is a separate optional delivery path.

For source-accessible games, a training integration should expose:

- An episode reset and seed, task identifier and success/failure events.
- Timestamped head/hand/controller actions; optional physical-body controls
  where the task needs them. Teleported controller poses are not automatically
  a physically realistic embodied agent.
- Stereo observations aligned to actions, plus a separate privileged state
  channel for rewards/debugging where appropriate.
- Explicit physics-step and observation scheduling, with pause/replay support.
  Determinism and accelerated stepping require implementation and validation;
  they do not follow merely from using a game engine.
- Locally staged assets, independent instance state and an offline startup path.

Game-level resets are particularly useful because our GPU environments do not
currently support restoring live graphics state. They supplement the existing
sandbox cold-filesystem restore rather than pretending that a gameplay save is
a complete environment snapshot.

There is research precedent for this direction:
[VRKitchen](https://github.com/xfgao/VRKitchen) supplied an Ubuntu environment
and Python examples for kitchen tool use and compositional tasks, though its
Ubuntu 16.04/Python 2.7 stack is old and untested here.
[Robo-Saber (2026)](https://arxiv.org/abs/2602.18319) studies generating/simulating
VR players on Beat Saber. These support the task concept, not this lab's runtime
compatibility or performance.

For distributable original content, [Freedoom](https://freedoom.github.io/about.html)
and [LibreQuake](https://github.com/lavenderdotpet/LibreQuake) avoid requiring the
original commercial game assets in their respective engine families.
[Poly Haven](https://polyhaven.com/license) offers CC0 art assets. In contrast,
freeware downloads, third-party music, proprietary game data and noncommercial
asset packs need their actual distribution terms carried into package choices.
No assets have been acquired or redistributed by this survey.

The earlier practical test order still makes sense: **Clone IT, FlightGear,
Locomancer**, retaining Open Saber as the known baseline. For extending source
capabilities, the most useful additional branches are **Luanti VR**, a
**LibreQuake/Freedoom VR package**, and a **small original manipulation/puzzle
game in Godot**. Commercial games broaden evaluation once their individual
store, runtime, input and offline constraints are resolved; they are not needed
to start building the core.

## Evidence and reproducibility

Primary material inspected included developer pages/repositories, official
Steam application metadata, runtime release notes and named firsthand reports
in LVRA DB. The old VR-on-Linux list was used for discovery only; its mixed
native/Proton grouping was not accepted as a platform guarantee.

Research fetches are ignored under `runs/vr-catalog-research/`: official Steam
metadata for 21 investigated apps, the LVRA index and selected report-page text.
The index contained 6,079 entries, many with **zero** reports. This is not a count
of compatible Linux games. The authored evidence distinctions and source links
are preserved in this committed note; downloaded metadata is supplemental.

Validation for this documentation-only change: review source-linked claims,
check local Markdown links and whitespace, review the staged diff, and check
both repository statuses. No runtime acceptance or new FPS claims are made.

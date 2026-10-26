Yes to all four. Sandweave runs on Modal, there is one action interface for every controller, there are separate observation types for sound, stereo VR video, haptics and game state, and pause works for every game. Speed control works fully only in Open Saber; slow motion and tick-exact steps on the desktop games are what I was building when you asked.

**Where it lives**
- **Sandweave**, branch `games` (`~/Developer/sandweave`, pushed to `Pranjal2041/sandweave`, latest `f790a01`): the runtime, action interface, observations and time control.
- **cua-auto-harness**, branch `games` (`gym-anything/cua-auto-harness`, `d21842b`): checks that gamepad and VR actions actually reach the game.
- **The five games' environments** come from `devin-datagen-lawrence-games/.devin/frontier-games` and run through gym-anything's Sandweave runner.

**Modal as provider: yes, two ways**
- **VR games** (Open Saber, `runtime="modal"`): the game runs in a Modal GPU sandbox (L40S). A Sandweave worker drives it through a relay, and both are placed in the same Modal region and cloud by default (about 1 ms round trip).
- **Desktop games** (Wesnoth, OpenTTD, OpenRA, Speed Dreams, `runtime="gvisor"`): each game runs in a gVisor sandbox inside a Modal worker.

**One action interface: yes**
All actions come from `sandweave.actions` and are applied with `sandweave.steps.run(sandbox, step)`:

| Device | Actions |
|---|---|
| desktop | click, press/release, move, drag, scroll, key chord, key down/up, type text |
| gamepad | `pad_button`, `pad_axis`: 4 pads, sticks, triggers; held until changed |
| xr | `xr_pose` (head and both hands), `xr_input` (trigger, grip, buttons, thumbstick) |
| time | `wait` (ms or ticks), `game_speed` |

**Observation types: yes**
Every observation carries the game's own clock time, the step ID and the index of the last action applied.

| Type | What it is | Where it works |
|---|---|---|
| `frame` | desktop screen | all games |
| `stereo` | paired left and right eye frames | VR, about 30 FPS |
| `audio` | the game's sound, 48 kHz | Modal only |
| `haptics` | VR controller vibration; gamepad rumble and force feedback | VR haptics on Modal; gamepad anywhere |
| `state` | each game's live state (score, date, cash, turn, race standings and so on) | all five games |

**Pause and speed: partly**

| | Open Saber (the game's own control) | Desktop games (frozen guest clock) |
|---|---|---|
| Pause | yes | yes |
| Exact waits | yes, in ticks | yes, in ms (1,000 ms measured as exactly 1,000.0) |
| Slow motion | yes | **not yet**, in progress |
| Faster than real time | yes, up to 4× | **no**: freezing the clock can only slow a game down |
| Tick-exact steps | yes | **not yet**, in progress |

On the desktop games, input sent while paused waits and costs no game time. OpenTTD's date stays frozen during a pause and moves exactly the right amount after a wait.

**Still open:**
- Slow motion and tick-exact steps for the desktop games.
- The gym-anything switch that creates sandboxes with the freezing clock is only in the worker's copy, not the gym-anything repo.
- The GunSpinning gamepad test needs its game archive, which I don't have yet.

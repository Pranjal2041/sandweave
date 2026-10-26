# Unified action and observation space

Goal: an agent anywhere (inside its own ordinary sandbox) drives an application
or game through one API. Every action and observation uses one vocabulary, so
runners, translations (pyautogui, model tool calls) and checkers are shared.
Schema: [`src/sandweave/actions.py`](../src/sandweave/actions.py); tests in
`tests/test_actions.py`.

## Vocabulary

The desktop part is the canonical vocabulary of
[cua-auto-harness](https://github.com/gym-anything/cua-auto-harness)
(`contracts.py`), unchanged in meaning, so its probes and checker apply directly.

| Group | Kinds | Style |
| --- | --- | --- |
| desktop | `click`, `button_press`, `button_release`, `move`, `drag`, `scroll`, `key_chord` (optional `hold_ms`), `key_down`, `key_up`, `type_text` | events |
| gamepad | `pad_button` (`down`, `up` or `press` with `hold_ms`), `pad_axis` (sticks −1..1, triggers 0..1) | events and state |
| xr | `xr_pose` (head/left/right position, unit quaternion, velocities), `xr_input` (buttons, trigger, squeeze, thumbstick) | state |
| time | `wait` (`ms` or game `ticks`) | — |

A step is `{actions: [...], observe: [...], id}`. The model may mix discrete
events with continuous state; state persists until changed. Scripts such as
pyautogui compile to these kinds instead of being a separate action type.
Names follow the qualified writers: gamepad buttons from
`scripts/gamepad-input.py`, XR inputs from Monado's remote driver
(`scripts/vr_input.py`).

## Time

`hold_ms` and `wait` are game time. `game_speed` (0 pauses, up to 4) sets the
speed of a game with time control; `steps.elapse` then advances exact physics
ticks from a pause (`wait` in ms converts at the game's tick rate) or waits
`ms / speed` of wall time while running. Without time control, game time is wall
time and `wait` in ticks raises `UnsupportedFeature`.

### Open Saber time control

Sandweave's Open Saber pack (`notes/open-saber-time-control.patch`, autoload
`SandweaveTime`) reads a control file every physics tick; `sandbox.vr.time(scale=,
advance_ticks=)` writes it through the relay and waits for the game's
acknowledgement. Pause pauses the scene tree (the song pauses with it; the frozen
scene keeps rendering), a speed scales physics and the song together, and an
advance runs exactly N ticks from a pause, then sets the song to the ticks
simulated (a slow frame let it run ahead: 30 ticks moved it 1.14 s in attempt 11).
Live on Modal (attempt 13): pausing for 2 s moved the song 0.012 s (one audio
buffer; the paused audio is digital silence after its first 100 ms), 90 ticks
moved it 1.003 s, a 30-tick step 0.348 s, and half speed ran it at 0.49996x.

## Observations

`observe` requests any of `frame`, `stereo`, `audio`, `haptics`, `state`.
Each returned observation is `{value, game_time_ns, step_id, last_action_index}`.
Game time is the guest's `CLOCK_MONOTONIC`, shared by every source:

| Observation | Source | `game_time_ns` |
| --- | --- | --- |
| `frame` / `stereo` | fast I/O frame / Monado paired-eye ring; after XR actions, the first frame captured after the last input acknowledgement | capture time |
| `audio` | `sandbox.audio`: PulseAudio null-sink monitor, s16le 48 kHz stereo, every frame since the previous read (`dropped_frames` if a reader falls more than 60 s behind) | receipt of the last returned frame |
| `haptics` | `sandbox.vr.haptics()`: vibration commands Monado's virtual controllers receive; `sandbox.gamepad.haptics()`: `SDL_JoystickRumble`/`RumbleTriggers` through the SDL proxy | time the records were read |
| `state` | `sandweave.games.state.GameState(game)`: the game's live state from the telemetry its environment already writes (below) | time the telemetry was read |

Audio and VR haptics are provided by `runtime="modal"` (its guest boot runs the
sound server; its Monado records haptics). Elsewhere they raise
`UnsupportedFeature`. Zero-amplitude VR commands are recorded only when they end a
vibration: Open Saber refreshes its command every frame, about 90% of them at
amplitude 0. A read returns at most 8,192 events (`complete: false` when more remain).

### Game state

Each frontier game environment already writes trusted, root-only telemetry for its
verifier. `src/sandweave/games/state_export.py` runs as root in the guest (installed
by `GameState` through the public SDK, so any runtime works), reads only what was
written since the previous call (cursor and aggregate cached in `/run/sandweave-state`)
and returns the game's current aggregate:

| Game | Telemetry (environment's own path) | State |
| --- | --- | --- |
| Speed Dreams | race journal `/var/lib/sdracer/eval/journal.jsonl` (`sd-2.4.2-eval-journal.patch`) | session and track, simulation time and multiplier history, the human car (lap, position, speed, gear, fuel, damage), standings, laps, session results |
| Open Saber | gameplay journal `<session>/private/journal-NN.jsonl` (`open_saber_journal.patch`) | current map and attempt, score, combo, multiplier, cut/miss/bomb/wall counts, poses, every attempt's result |
| OpenTTD | `GACR` records in `/var/lib/ottd_task/run/game.log` (game script) | date, pause, every company (value, bank, loan, vehicles, income, expenses, cargo), last 24 quarters, events, setting changes, end |
| Wesnoth | audit plugin lines in `/var/lib/wesnoth-audit/game.log` | context, scenario, turn, level result, each scenario's results, game restarts, debug mode |
| OpenRA | newest replay under `/var/lib/openra/support/Replays`, read with the environment's `openra_replay.py`; with `resim=True` re-simulated by the environment's hash-checked headless copy (`resim_replay.sh`, about 40x real time) | game time, players and bots, orders by kind and by client; with resim, each player's cash, structures, army and asset value, kills and losses, win state |
| Open Saber (Sandweave template) | the same journal from Sandweave's own pack, `$XDG_RUNTIME_DIR/open-saber/journal-NN.jsonl` | as above |

`scripts/game-state-check.py` replays each game's recorded telemetry into a real
Linux guest (gVisor `coding` sandbox on the Modal worker) in six chunks cut
mid-line and observes through the SDK after each. On 2026-10-25, all five passed:
every byte was consumed once, the last incremental state equalled a fresh full
read, and guest time increased. Sources were the Speed Dreams eval journal (13 MB),
Open Saber eval journal 02 (50 MB), an OpenTTD audit 3-hour game log, the Wesnoth
eval-02 audit log and the OpenRA eval replay. The states match the runs: Wesnoth
won The Elves Besieged on turn 11 and Blackwater Port on turn 10 and was on turn
10 of The Isle of Alduin; the Speed Dreams player was 10th of 10 after 82
simulation-speed changes; OpenTTD's period ended on 1965-05-29 with four companies;
OpenRA had 4 players (3 bots) and 384.7 s of game time.

Live play (2026-10-26, `scripts/game-state-live-check.py`, each environment's task
started with gym-anything on a worker from its frontier worker's exit snapshot,
state read every 30 s for 4 min as the game ran): all four desktop games passed.
OpenTTD's date advanced after unpausing with four companies; OpenRA's re-simulated
world followed the bots (cash 4,671 to 977, structures 2 to 9, army 0 to 1,800
over 159 s of game time); Speed Dreams' qualifying ran from t = -2 s to 208 s with
ten cars in the standings; Wesnoth's turn went from 1 to 2 when the turn was
ended. Open Saber's live state is read by the Modal acceptance from Sandweave's
pack (attempt 11: song 2.6 s, 8.5 s after the time tests, then the map's result,
score 384). Wesnoth takes input through gym-anything's Sandweave runner (fast I/O),
as the evaluation's solver played it (31 End Turn clicks at the same point):
`scripts/wesnoth-input-check.py` showed that the first click after the hand-over
is ignored, with the window focused and the pointer already on the button; a second
click opens "You have not started your turn yet. Do you really want to end your
turn?", and Enter accepts it (turn 1 to 2). An earlier run that sent one click and
Enter saw no change for that reason.

## Portfolio

| Game | Actions | Observations |
| --- | --- | --- |
| Wesnoth, OpenTTD | desktop clicks, drags, hotkeys | frame |
| OpenRA | desktop, including drag-select and held modifiers | frame |
| Speed Dreams | held keys or `pad_axis` steer/throttle/brake | frame, haptics |
| Open Saber | `xr_pose` head and hands, `xr_input` triggers | stereo, haptics |

## Execution

`sandweave.steps.run(sandbox, step)` applies a step on the sandbox's controls:
desktop actions become fast I/O batches (split only where a hold elapses), XR
actions merge into one VR state update, and gamepad actions set the persistent
leased writer's state (a timed press sets, then releases), on pads 0-3, `game_speed`
uses the game's time control and waits and holds elapse in game time (above).
Rejected with `UnsupportedFeature` rather than approximated: `game_speed` and
`wait` in ticks without time control. Observing `state` needs the game's
exporter: `steps.run(sandbox, step, state=GameState(game))`.
Tests: `tests/test_steps.py`, `tests/test_game_observations.py`.

On `runtime="modal"`, frames, input, audio and haptics travel through the guest
relay; see [modal-runtime.md](modal-runtime.md) for measured rates and placement.

## Gamepad

`scripts/gamepad-input.py --serve` holds buttons and axes until changed under a
1 s lease renewed every 0.25 s; end of input leaves the pad neutral. The SDL proxy
(`scripts/sdl-gamepad-proxy.c`, dynapi header pinned at SDL 2.30.9, whose Linux
slot list keeps 2.0.8's as an exact prefix) advertises rumble and records each
request. `scripts/sdl-gamepad-proxy-check.py` drives both against a real SDL2
library: on the Modal worker (Ubuntu `libsdl2-2.0-0` 2.30.0) a button and axis
held 2.88 s past the lease, a release kept the axis, end of input returned the
pad to neutral, and all three rumble requests (motors, triggers, stop) came back
through the writer.

Triggers `lt`/`rt` are raw axes 2 and 5 (0..1, -32768 released), as SDL reports an
Xbox 360 pad on Linux and SDL's controller database maps this GUID; the neutral
state releases them. `LAB_GAMEPAD_COUNT` pads (1-4) read `LAB_GAMEPAD_STATE` and
`<that>-N`; the capability's `pads` starts one writer per pad. Each pad also has
an SDL_Haptic device: `SDL_HapticRumblePlay`/`Stop` and constant-force effects are
recorded (`haptic_rumble`, `constant_force` in `gamepad.haptics()`). The SDL check
passed with two pads, triggers and both kinds of force feedback.

In a running game (`scripts/gamepad-speed-dreams-check.py`): Speed Dreams 2.4.2,
the frontier environment's qualifying session, restarted with the proxy and the
player's throttle, brake and steering on right trigger, left trigger and left
stick. The game's own commands in its race journal followed the pad (throttle 1.0,
brake 1.0, steer 0.6 for the stick at -0.6, all 0 at release), the car went from
9.5 to 27.5 m/s under throttle and stopped under brake, and the game's force
feedback came back: the start-up rumble at 0.5 and its constant force while
steering is on the pad (1,277 records). The writer ran as root, since the
environment's lockdown denies its game user `python3`; the state file is 0644.

## Checks (cua-auto-harness)

The `games` branch of cua-auto-harness (`d21842b`) checks these actions through
this runner: canonical `pad_button`, `pad_axis`, `xr_pose`, `xr_input`; a gamepad
probe that reads every pad through the guest's libSDL2 with the proxy loaded, as a
game does; Sandweave's in-app XR probe (`notes/open-saber-xr-probe.patch`, autoload
`SandweaveXRProbe`, recording what the OpenXR app receives while
`$XDG_RUNTIME_DIR/open-saber/xr-probe.jsonl.enable` exists); and adapters
`sandweave-gamepad` (GNOME sandbox, two pads) and `sandweave-xr` (Open Saber on
`runtime="modal"`) that inject with `steps.run`. On 2026-10-26 on the Modal worker,
every entry passed all five repetitions, each observing a change: gamepad 15/15
(presses held 604 ms for 600, 1,506 ms for a 1.5 s down/up past the lease; sticks,
triggers, pad 1) and XR 11/11 (10 cm moves within 1e-8 m, a -20 degree head yaw
within 0.02 degrees, trigger, squeeze, A button, thumbstick).

## Game time on desktop games

Any game on `runtime="gvisor"` gets exact game time from a sandbox created with
`clock_pause="freeze"` (`FrozenGuestClock` in `steps.py`, `Sandbox.game_time`):

- `game_speed` 0 freezes the guest, wall and monotonic clocks included, with its
  controls attached; 1 thaws it. The frame observed while frozen is captured as it
  freezes.
- A `wait` from a freeze runs the guest for exactly that much guest time: the engine
  (`runsc resume --for=D`, capability `clock_advance` 2) stops guest time at the
  deadline before it stops the tasks, so no task observes a later time.
- Input given while frozen waits in the guest and lands as the next advance starts;
  the advance holds the operations lock shared, so fast I/O reaches the guest while
  it runs. The frame observed after an advance is captured in its last 50 ms.
- Other speeds and tick waits need the game's own time control (Open Saber's above).

Live on the Modal worker (`scripts/frozen-clock-check.py`, GNOME sandbox, a guest loop
reading `CLOCK_MONOTONIC` every 50 ms), three runs: every 1000 ms advance measured
exactly 1000.0 ms of guest time, the click queued while frozen was delivered inside
it, the guest's largest step was 52.4-53.1 ms while 9 s of freezes passed, and its
elapsed time matched its running time within the freeze/thaw round trips.

In a real game (`scripts/openttd-game-time-check.py`, the frontier OpenTTD task with
gym-anything's Sandweave runner creating the sandbox with `clock_pause="freeze"`):
during 150 s of host time frozen the guest's uptime moved 0.31 s and the game date
stayed 1950-01-05; an advance of exactly 150000.0 ms (guest uptime +150.2 s with the
reads) moved the date to the 1950-03-01 record, two month starts. The runner's opt-in
(`GYM_SANDWEAVE_CLOCK_PAUSE=freeze`) was applied in the worker's gym-anything checkout.

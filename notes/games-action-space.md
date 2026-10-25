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

`hold_ms` and `wait` are game time. With pause or slowed time, a 120 ms hold
remains 120 ms of game time. Speed control itself (pause, slow, tick stepping)
is a runtime capability; Sandweave's engine provides pause and guest clocks,
while `runtime="modal"` does not yet.

## Observations

`observe` requests any of `frame`, `stereo`, `audio`, `haptics`, `state`.
Each returned observation is `{value, game_time_ns, step_id, last_action_index}`.
Game time is the guest's `CLOCK_MONOTONIC`, shared by every source:

| Observation | Source | `game_time_ns` |
| --- | --- | --- |
| `frame` / `stereo` | fast I/O frame / Monado paired-eye ring; after XR actions, the first frame captured after the last input acknowledgement | capture time |
| `audio` | `sandbox.audio`: PulseAudio null-sink monitor, s16le 48 kHz stereo, every frame since the previous read (`dropped_frames` if a reader falls more than 60 s behind) | receipt of the last returned frame |
| `haptics` | `sandbox.vr.haptics()`: vibration commands Monado's virtual controllers receive; `sandbox.gamepad.haptics()`: `SDL_JoystickRumble`/`RumbleTriggers` through the SDL proxy | time the records were read |
| `state` | not implemented: needs a per-game export | — |

Audio and VR haptics are provided by `runtime="modal"` (its guest boot runs the
sound server; its Monado records haptics). Elsewhere they raise
`UnsupportedFeature`. Zero-amplitude VR commands are recorded only when they end a
vibration: Open Saber refreshes its command every frame, about 90% of them at
amplitude 0. A read returns at most 8,192 events (`complete: false` when more remain).

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
leased writer's state (a timed press sets, then releases). Rejected with
`UnsupportedFeature` rather than approximated: `wait` in ticks and holds in
slowed time (no runtime speed control yet), gamepad trigger axes (`lt`/`rt`,
calibration unresolved), pads other than 0, and the `state` observation.
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
through the writer. This is a real SDL program, not a game: the only gamepad
template (GunSpinning) runs on gVisor and has not been run with this writer.

## Not implemented

- Runtime speed control (pause, slow, tick stepping) on Modal.
- The `state` observation (per-game export).
- cua-auto-harness probes and checks for gamepad and XR actions.

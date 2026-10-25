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
Each returned observation will carry game time and the ID of the last applied
action, so an agent knows which inputs it reflects.

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
actions merge into one VR state update, and gamepad presses use the leased
writer. Rejected with `UnsupportedFeature` rather than approximated: persistent
gamepad state (the writer releases after each hold), `wait` in ticks and holds
in slowed time (no runtime speed control yet), and `audio`, `haptics` and
`state` observations. Tests: `tests/test_steps.py`.

On `runtime="modal"`, frames and input travel through the guest relay; see
[modal-runtime.md](modal-runtime.md) for its measured rates.

## Not implemented

- Runtime speed control (pause, slow, tick stepping) on Modal.
- Game time and last action ID on each observation.
- cua-auto-harness probes and checks for gamepad and XR actions.

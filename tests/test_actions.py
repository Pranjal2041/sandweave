import pytest

from sandweave.actions import (ACTIONS, KeyChord, PadAxis, Step, Wait, XRPose, action_from_dict, action_to_dict)

EXAMPLES = [
    {'kind': 'click', 'x': 10, 'y': 20, 'button': 'right', 'count': 2, 'modifiers': ['shift']},
    {'kind': 'button_press', 'x': 1, 'y': 2}, {'kind': 'button_release', 'x': 1, 'y': 2},
    {'kind': 'move', 'x': 5, 'y': 6}, {'kind': 'drag', 'path': [[0, 0], [40, 30]], 'modifiers': ['ctrl']},
    {'kind': 'scroll', 'x': 3, 'y': 4, 'dy_ticks': -2}, {'kind': 'key_chord', 'keys': ['ctrl', 's'], 'hold_ms': 50},
    {'kind': 'key_down', 'key': 'up'}, {'kind': 'key_up', 'key': 'up'}, {'kind': 'type_text', 'text': 'hello'},
    {'kind': 'pad_button', 'button': 'a', 'hold_ms': 120}, {'kind': 'pad_axis', 'axis': 'rt', 'value': .8},
    {'kind': 'xr_pose', 'device': 'right', 'position': [.2, 1.3, -.5], 'orientation': [0, 0, 0, 1]},
    {'kind': 'xr_input', 'device': 'left', 'buttons': {'trigger_click': True}, 'trigger_value': 1},
    {'kind': 'wait', 'ticks': 2},
]


def test_every_kind_round_trips():
    assert {example['kind'] for example in EXAMPLES} == set(ACTIONS)
    for example in EXAMPLES:
        action = action_from_dict(dict(example))
        assert action_from_dict(action_to_dict(action)) == action


@pytest.mark.parametrize('value', [
    {'kind': 'teleport'}, {'kind': 'click', 'x': -1, 'y': 0}, {'kind': 'click', 'x': 1, 'y': 1, 'extra': 1},
    {'kind': 'key_chord', 'keys': ['s', 'ctrl']}, {'kind': 'key_down', 'key': 'hyper'},
    {'kind': 'pad_axis', 'axis': 'lt', 'value': -.5}, {'kind': 'pad_axis', 'axis': 'lx', 'value': 1.5},
    {'kind': 'pad_button', 'button': 'a', 'state': 'down', 'hold_ms': 10},
    {'kind': 'xr_pose', 'device': 'head', 'orientation': [0, 0, 0, 2]}, {'kind': 'xr_pose', 'device': 'head'},
    {'kind': 'xr_input', 'device': 'head', 'trigger_value': 1}, {'kind': 'wait', 'ms': 10, 'ticks': 1},
    {'kind': 'scroll', 'x': 0, 'y': 0}, {'kind': 'drag', 'path': [[0, 0]]},
])
def test_invalid_actions_are_rejected(value):
    with pytest.raises((ValueError, TypeError)):
        action_from_dict(value)


def test_steps_mix_events_and_state_per_game():
    racing = Step([PadAxis('lx', -.3), PadAxis('rt', 1), Wait(ms=100)], observe=['frame', 'haptics'], id='s1')
    saber = Step([XRPose('head', position=[0, 1.6, 0]),
                  {'kind': 'xr_input', 'device': 'right', 'trigger_value': 1}], observe=['stereo'])
    strategy = Step([{'kind': 'drag', 'path': [[100, 100], [300, 250]]}, KeyChord(['ctrl', '1'])])
    assert racing.devices() == {'gamepad', 'time'} and saber.devices() == {'xr'} and strategy.devices() == {'desktop'}
    assert Step.from_dict(racing.to_dict()) == racing
    with pytest.raises(ValueError):
        Step([], observe=['smell'])
    with pytest.raises(ValueError):
        Step.from_dict({'actions': [], 'observe': ['frame'], 'speed': 2})

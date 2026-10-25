import types

import pytest

from sandweave.actions import action_from_dict
from sandweave.sandbox.errors import UnsupportedFeature
from sandweave.steps import desktop_segments, run, xr_state


def actions(*values):
    return [action_from_dict(value) for value in values]


def test_desktop_actions_become_fast_io_batches_split_at_holds():
    segments = desktop_segments(actions(
        {'kind': 'click', 'x': 10, 'y': 20, 'count': 2, 'modifiers': ['shift']},
        {'kind': 'drag', 'path': [[0, 0], [5, 5]], 'button': 'middle'},
        {'kind': 'scroll', 'x': 1, 'y': 2, 'dy_ticks': 3},
        {'kind': 'key_chord', 'keys': ['ctrl', 'kp_add'], 'hold_ms': 50},
        {'kind': 'type_text', 'text': 'hi'}))
    assert len(segments) == 2 and segments[0][1] == 50 and segments[1][1] == 0
    first = segments[0][0]
    assert first[:3] == [{'keyboard': {'keys_down': ['shift']}}, {'mouse': {'double_click': [10, 20]}},
                         {'keyboard': {'keys_up': ['shift']}}]
    assert {'mouse': {'move': [0, 0], 'buttons': 'middle_down'}} in first
    assert {'mouse': {'move': [1, 2], 'scroll': {'dx': 0, 'dy': 3}}} in first
    assert first[-1] == {'keyboard': {'keys_down': ['ctrl', 'KP_Add']}}
    assert segments[1][0] == [{'keyboard': {'keys_up': ['ctrl', 'KP_Add']}}, {'keyboard': {'text': 'hi'}}]


def test_xr_actions_merge_into_one_state_update():
    state = xr_state(actions(
        {'kind': 'xr_pose', 'device': 'right', 'position': [.2, 1.3, -.5], 'orientation': [0, 0, 0, 1]},
        {'kind': 'xr_input', 'device': 'right', 'buttons': {'trigger_click': True}, 'trigger_value': 1},
        {'kind': 'xr_pose', 'device': 'head', 'position': [0, 1.6, 0]}))
    assert state == {'right': {'position': [.2, 1.3, -.5], 'orientation': [0, 0, 0, 1], 'trigger_click': True,
                               'trigger_value': 1}, 'head': {'position': [0, 1.6, 0]}}


def test_run_dispatches_in_order_and_observes():
    calls = []
    vr = types.SimpleNamespace(action=lambda state: calls.append(('vr', state)) or {'ack': 'vr'},
                               observe=lambda: 'stereo-frame')
    gamepad = types.SimpleNamespace(action=lambda buttons, hold: calls.append(('pad', buttons, hold)) or {},
                                    state=lambda **update: calls.append(('pad_state', update)) or {})
    sandbox = types.SimpleNamespace(capabilities={'vr': {}}, vr=vr, gamepad=gamepad,
                                    desktop=types.SimpleNamespace(action=lambda items: calls.append(('desktop', items))))
    result = run(sandbox, {'id': 's1', 'observe': ['stereo'], 'actions': [
        {'kind': 'xr_pose', 'device': 'left', 'position': [0, 1, 0]},
        {'kind': 'pad_button', 'button': 'a', 'hold_ms': 100},
        {'kind': 'xr_input', 'device': 'left', 'trigger_value': .5},
        {'kind': 'wait', 'ms': 1}]})
    assert [call[0] for call in calls] == ['vr', 'pad', 'vr'] and calls[1] == ('pad', ['a'], .1)
    assert result['id'] == 's1' and result['observations'] == {'stereo': 'stereo-frame'}
    assert result['actions_applied'] == 4
    calls.clear()
    run(sandbox, {'actions': [{'kind': 'pad_axis', 'axis': 'lx', 'value': .5},
                              {'kind': 'pad_button', 'button': 'b', 'state': 'down'}], 'observe': []})
    assert calls == [('pad_state', {'axes': {'lx': .5}}), ('pad_state', {'buttons_down': ['b']})]
    with pytest.raises(UnsupportedFeature):
        run(sandbox, {'actions': [{'kind': 'pad_axis', 'axis': 'rt', 'value': .5}], 'observe': []})
    with pytest.raises(UnsupportedFeature):
        run(sandbox, {'actions': [{'kind': 'wait', 'ticks': 2}], 'observe': []})
    with pytest.raises(UnsupportedFeature):
        run(sandbox, {'actions': [], 'observe': ['audio']})

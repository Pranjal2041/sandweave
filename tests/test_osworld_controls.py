"""Compare input commands with a separately supplied, read-only reference."""
import ast
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from sandweave.templates.osworld.controls import AttachedXorg


def test_combined_buttons_preserve_press_then_release_order():
    desktop = object.__new__(AttachedXorg)
    desktop.config = {}
    calls = []
    desktop.run_user = lambda command, **kw: calls.append(command)
    desktop.inject({'mouse': {'buttons': {'left_up': True, 'right_down': True}, 'scroll': -3}})
    assert calls == ['xdotool mousedown 3', 'xdotool mouseup 1', 'xdotool click 4 click 4 click 4 ']


def test_mouse_commands_match_pinned_modal_native_reference():
    source = os.environ.get('SANDWEAVE_OSWORLD_SOURCE')
    if source is None:
        pytest.skip('set SANDWEAVE_OSWORLD_SOURCE to the pinned reference checkout')
    path = Path(source) / 'src/cua_speedrun/envs/modal_native.py'
    tree = ast.parse(path.read_text())
    adapter = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'ModalNativeAdapter')
    method = next(node for node in adapter.body if isinstance(node, ast.FunctionDef) and node.name == '_inject')
    module = ast.Module(body=[method], type_ignores=[])
    namespace = {'Any': object}
    exec(compile(module, str(path), 'exec'), namespace)
    commands = []
    reference = SimpleNamespace(_run_user=lambda command, **kw: commands.append(command))
    actual = object.__new__(AttachedXorg)
    actual.config = {}
    outputs = []
    actual.run_user = lambda command, **kw: outputs.append(command)
    actions = [
        {'mouse': {'left_click': [10, 20], 'right_click': [30, 40], 'middle_click': [50, 60]}},
        {'mouse': {'double_click': [1, 2], 'triple_click': [3, 4], 'move': [5, 6]}},
        {'mouse': {'left_click_drag': [[1, 2], [30, 40]], 'right_click_drag': [[80, 90]]}},
        {'mouse': {'buttons': {button + '_' + direction: True
                   for button in ('left', 'right', 'middle') for direction in ('down', 'up')}}},
        {'mouse': {'scroll': -4}}, {'mouse': {'scroll': 3}},
    ]
    for action in actions:
        namespace['_inject'](reference, action)
        actual.inject(action)
    assert outputs == commands

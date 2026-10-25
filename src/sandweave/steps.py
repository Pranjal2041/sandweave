"""Execute unified steps (sandweave.actions) on a sandbox's existing controls.

Desktop actions become fast I/O batches (one fenced batch per run of desktop
actions, split only where a hold must elapse). XR actions in a step merge into
one VR state update. Gamepad actions use the qualified leased writer, which
holds buttons and axes for a duration and then releases them; persistent
gamepad state is rejected rather than approximated. Until a runtime provides
speed control, waits and holds elapse in wall time, which equals game time.
"""
import time

from .actions import Step, XR_SCALARS
from .sandbox.errors import UnsupportedFeature

KEYS = {'caps_lock': 'Caps_Lock', 'num_lock': 'Num_Lock', 'scroll_lock': 'Scroll_Lock', 'menu': 'Menu',
        'kp_enter': 'KP_Enter', 'kp_add': 'KP_Add', 'kp_subtract': 'KP_Subtract', 'kp_multiply': 'KP_Multiply',
        'kp_divide': 'KP_Divide', **{f'kp_{n}': f'KP_{n}' for n in range(10)}}


def _keys(names):
    return [KEYS.get(name, name) for name in names]


def desktop_segments(actions):
    """Fast I/O batches and the pauses between them: [(items, pause_ms), ...]."""
    segments, items = [], []
    for action in actions:
        kind = action.kind
        if kind == 'click':
            if action.modifiers:
                items.append({'keyboard': {'keys_down': _keys(action.modifiers)}})
            name = {2: 'double_click', 3: 'triple_click'}.get(action.count) if action.button == 'left' else None
            for _ in range(1 if name else action.count):
                items.append({'mouse': {name or action.button + '_click': [action.x, action.y]}})
            if action.modifiers:
                items.append({'keyboard': {'keys_up': _keys(action.modifiers)}})
        elif kind in ('button_press', 'button_release'):
            direction = 'down' if kind == 'button_press' else 'up'
            items.append({'mouse': {'move': [action.x, action.y], 'buttons': f'{action.button}_{direction}'}})
        elif kind == 'move':
            items.append({'mouse': {'move': [action.x, action.y]}})
        elif kind == 'drag':
            if action.modifiers:
                items.append({'keyboard': {'keys_down': _keys(action.modifiers)}})
            path = [list(point) for point in action.path]
            if action.button in ('left', 'right'):
                items.append({'mouse': {action.button + '_click_drag': path}})
            else:
                items.append({'mouse': {'move': path[0], 'buttons': f'{action.button}_down'}})
                items += [{'mouse': {'move': point}} for point in path[1:]]
                items.append({'mouse': {'buttons': f'{action.button}_up'}})
            if action.modifiers:
                items.append({'keyboard': {'keys_up': _keys(action.modifiers)}})
        elif kind == 'scroll':
            items.append({'mouse': {'move': [action.x, action.y],
                                    'scroll': {'dx': action.dx_ticks, 'dy': action.dy_ticks}}})
        elif kind == 'key_chord':
            if action.hold_ms is None:
                items.append({'keyboard': {'keys': _keys(action.keys)}})
            else:
                items.append({'keyboard': {'keys_down': _keys(action.keys)}})
                segments.append((items, action.hold_ms))
                items = [{'keyboard': {'keys_up': _keys(action.keys)}}]
        elif kind in ('key_down', 'key_up'):
            items.append({'keyboard': {'keys_' + kind[4:]: _keys([action.key])}})
        elif kind == 'type_text':
            items.append({'keyboard': {'text': action.text}})
        else:
            raise ValueError('not a desktop action: ' + kind)
    if items:
        segments.append((items, 0))
    return segments


def xr_state(actions):
    """Merge XR actions into one Monado remote-driver state update."""
    state = {}
    for action in actions:
        device = state.setdefault(action.device, {})
        if action.kind == 'xr_pose':
            for name in ('position', 'orientation', 'linear_velocity', 'angular_velocity'):
                if getattr(action, name) is not None:
                    device[name] = list(getattr(action, name))
        else:
            device.update(action.buttons)
            for name in (*XR_SCALARS, 'thumbstick'):
                if getattr(action, name) is not None:
                    device[name] = getattr(action, name)
    return state


def _gamepad(sandbox, action):
    if action.pad != 0:
        raise UnsupportedFeature('the gamepad writer drives pad 0')
    if action.kind == 'pad_button':
        if action.state == 'press':
            return sandbox.gamepad.action(buttons=[action.button], hold=(action.hold_ms or 200) / 1000)
        return sandbox.gamepad.state(**{'buttons_' + action.state: [action.button]})
    if action.axis in ('lt', 'rt'):
        raise UnsupportedFeature('trigger axes are not calibrated for the gamepad writer')
    return sandbox.gamepad.state(axes={action.axis: action.value})


def run(sandbox, step):
    """Apply a step's actions in order, then collect its observations."""
    step = step if isinstance(step, Step) else Step.from_dict(step)
    acknowledgements, group, started = [], [], time.monotonic_ns()

    def flush():
        if not group:
            return
        kind = group[0].kind
        if kind.startswith('xr_'):
            acknowledgements.append(sandbox.vr.action(xr_state(group)))
        else:
            for items, pause_ms in desktop_segments(group):
                acknowledgements.append(sandbox.desktop.action(items))
                if pause_ms:
                    time.sleep(pause_ms / 1000)
        group.clear()

    for action in step.actions:
        family = 'xr' if action.kind.startswith('xr_') else 'pad' if action.kind.startswith('pad_') else \
            'wait' if action.kind == 'wait' else 'desktop'
        if group and ('xr' if group[0].kind.startswith('xr_') else 'desktop') != family:
            flush()
        if family in ('xr', 'desktop'):
            group.append(action)
        elif family == 'pad':
            acknowledgements.append(_gamepad(sandbox, action))
        elif action.ms is not None:
            time.sleep(action.ms / 1000)
        else:
            raise UnsupportedFeature('wait in game ticks requires runtime speed control')
    flush()
    observations, capabilities = {}, sandbox.capabilities
    for name in step.observe:
        if name == 'stereo' or (name == 'frame' and 'desktop' not in capabilities):
            observations[name] = sandbox.vr.observe()
        elif name == 'frame':
            observations[name] = sandbox.desktop.capture_image()
        else:
            raise UnsupportedFeature(f'the {name!r} observation is not provided by this sandbox yet')
    return {'id': step.id, 'acknowledgements': acknowledgements, 'observations': observations,
            'actions_applied': len(step.actions), 'started_ns': started, 'finished_ns': time.monotonic_ns()}

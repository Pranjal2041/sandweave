"""Unified action and observation space for desktop applications and games.

Extends cua-auto-harness's canonical desktop vocabulary (click, button_press,
button_release, move, drag, scroll, key_chord, type_text, wait) with key holds,
gamepad and XR actions, so one checker can verify every kind of input.

A step is an ordered list of actions. A model may mix discrete events (key
down/up, button presses) with continuous state (gamepad axes, XR poses and
inputs), which stays in effect until changed.

Conventions: coordinates are guest screen pixels, origin top-left; positive
dy_ticks scrolls down; key names are lowercase canonical names and a chord
lists modifiers first. Durations (hold_ms, wait) are game time, so pausing or
slowing a game stretches them in wall time.
"""
from dataclasses import asdict, dataclass, field, fields
import math

BUTTONS = ('left', 'middle', 'right', 'back', 'forward')
MODIFIERS = ('ctrl', 'shift', 'alt', 'super')
KEY_NAMES = (
    *MODIFIERS, 'enter', 'esc', 'tab', 'space', 'backspace', 'delete', 'insert',
    'up', 'down', 'left', 'right', 'home', 'end', 'pageup', 'pagedown',
    *(f'f{n}' for n in range(1, 13)),
    'minus', 'equal', 'grave', 'backslash', 'slash', 'semicolon', 'apostrophe',
    'comma', 'period', 'bracketleft', 'bracketright',
    'caps_lock', 'num_lock', 'scroll_lock', 'print', 'pause', 'menu',
    *(f'kp_{n}' for n in range(10)), 'kp_add', 'kp_subtract', 'kp_multiply', 'kp_divide', 'kp_enter',
)
# Names of the qualified gamepad writer (scripts/gamepad-input.py); the d-pad is up/down/left/right.
PAD_BUTTONS = ('a', 'b', 'x', 'y', 'lb', 'rb', 'back', 'start', 'menu', 'ls', 'rs', 'up', 'down', 'left', 'right')
PAD_STICKS, PAD_TRIGGERS = ('lx', 'ly', 'rx', 'ry'), ('lt', 'rt')
XR_DEVICES, XR_HANDS = ('head', 'left', 'right'), ('left', 'right')
# Monado remote-driver controller inputs (scripts/vr_input.py).
XR_BUTTONS = tuple(f'{name}_{event}' for name in ('a', 'b', 'system', 'trigger', 'thumbstick')
                   for event in ('click', 'touch'))
XR_SCALARS = ('trigger_value', 'squeeze_value')
OBSERVATIONS = ('frame', 'stereo', 'audio', 'haptics', 'state')
MAX_HOLD_MS = 10_000


def _key(name):
    if not isinstance(name, str) or not (name in KEY_NAMES or (len(name) == 1 and name.isprintable() and name != ' ')):
        raise ValueError(f'unknown key: {name!r}')


def _point(x, y):
    if any(type(v) is not int or v < 0 for v in (x, y)):
        raise ValueError('coordinates must be non-negative integer pixels')


def _hold(hold_ms, *, optional=True):
    if hold_ms is None and optional:
        return
    if not isinstance(hold_ms, (int, float)) or not 0 < hold_ms <= MAX_HOLD_MS:
        raise ValueError(f'hold_ms must be in (0, {MAX_HOLD_MS}]')


def _vector(value, length, name, *, bound=None):
    if (not isinstance(value, (list, tuple)) or len(value) != length or
            any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in value)):
        raise ValueError(f'{name} must be {length} finite numbers')
    if bound is not None and any(abs(v) > bound for v in value):
        raise ValueError(f'{name} components must be within ±{bound}')


@dataclass
class Click:
    x: int
    y: int
    button: str = 'left'
    count: int = 1
    modifiers: list = field(default_factory=list)
    kind: str = 'click'

    def __post_init__(self):
        _point(self.x, self.y)
        if self.button not in BUTTONS or self.count not in (1, 2, 3):
            raise ValueError('click needs a known button and count 1..3')
        if any(m not in MODIFIERS for m in self.modifiers):
            raise ValueError('unknown click modifier')


@dataclass
class ButtonPress:
    x: int
    y: int
    button: str = 'left'
    kind: str = 'button_press'

    def __post_init__(self):
        _point(self.x, self.y)
        if self.button not in BUTTONS:
            raise ValueError('unknown mouse button')


@dataclass
class ButtonRelease(ButtonPress):
    kind: str = 'button_release'


@dataclass
class Move:
    x: int
    y: int
    kind: str = 'move'

    def __post_init__(self):
        _point(self.x, self.y)


@dataclass
class Drag:
    path: list
    button: str = 'left'
    modifiers: list = field(default_factory=list)
    kind: str = 'drag'

    def __post_init__(self):
        if not isinstance(self.path, (list, tuple)) or len(self.path) < 2:
            raise ValueError('drag path needs at least two points')
        for point in self.path:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError('drag points are [x, y]')
            _point(*point)
        if self.button not in BUTTONS or any(m not in MODIFIERS for m in self.modifiers):
            raise ValueError('drag needs a known button and modifiers')


@dataclass
class Scroll:
    x: int
    y: int
    dx_ticks: int = 0
    dy_ticks: int = 0
    kind: str = 'scroll'

    def __post_init__(self):
        _point(self.x, self.y)
        if type(self.dx_ticks) is not int or type(self.dy_ticks) is not int or not (self.dx_ticks or self.dy_ticks):
            raise ValueError('scroll needs nonzero integer ticks')


@dataclass
class KeyChord:
    keys: list
    hold_ms: float = None
    kind: str = 'key_chord'

    def __post_init__(self):
        if not isinstance(self.keys, (list, tuple)) or not self.keys:
            raise ValueError('key_chord needs at least one key')
        for name in self.keys:
            _key(name)
        if any(name not in MODIFIERS for name in self.keys[:-1]):
            raise ValueError('a chord lists modifiers first and its main key last')
        _hold(self.hold_ms)


@dataclass
class KeyDown:
    key: str
    kind: str = 'key_down'

    def __post_init__(self):
        _key(self.key)


@dataclass
class KeyUp(KeyDown):
    kind: str = 'key_up'


@dataclass
class TypeText:
    text: str
    kind: str = 'type_text'

    def __post_init__(self):
        if not isinstance(self.text, str) or not self.text:
            raise ValueError('type_text needs nonempty text')


@dataclass
class PadButton:
    button: str
    state: str = 'press'
    hold_ms: float = None
    pad: int = 0
    kind: str = 'pad_button'

    def __post_init__(self):
        if self.button not in PAD_BUTTONS or self.state not in ('down', 'up', 'press'):
            raise ValueError('pad_button needs a known button and state down, up or press')
        if self.state != 'press' and self.hold_ms is not None:
            raise ValueError('hold_ms applies only to press')
        _hold(self.hold_ms)
        if type(self.pad) is not int or not 0 <= self.pad < 4:
            raise ValueError('pad must be 0..3')


@dataclass
class PadAxis:
    """Continuous state: sticks in -1..1, triggers in 0..1, held until changed."""
    axis: str
    value: float
    pad: int = 0
    kind: str = 'pad_axis'

    def __post_init__(self):
        low = 0 if self.axis in PAD_TRIGGERS else -1
        if self.axis not in PAD_STICKS + PAD_TRIGGERS:
            raise ValueError(f'unknown pad axis: {self.axis!r}')
        if not isinstance(self.value, (int, float)) or not low <= self.value <= 1:
            raise ValueError(f'{self.axis} value must be in {low}..1')
        if type(self.pad) is not int or not 0 <= self.pad < 4:
            raise ValueError('pad must be 0..3')


@dataclass
class XRPose:
    """Continuous state of the head or a controller: metres, unit quaternion (x, y, z, w), velocities."""
    device: str
    position: list = None
    orientation: list = None
    linear_velocity: list = None
    angular_velocity: list = None
    kind: str = 'xr_pose'

    def __post_init__(self):
        if self.device not in XR_DEVICES:
            raise ValueError('xr_pose device must be head, left or right')
        values = {'position': 3, 'orientation': 4, 'linear_velocity': 3, 'angular_velocity': 3}
        if all(getattr(self, name) is None for name in values):
            raise ValueError('xr_pose needs at least one component')
        for name, length in values.items():
            if getattr(self, name) is not None:
                _vector(getattr(self, name), length, name)
        if self.orientation is not None and abs(math.sqrt(sum(v * v for v in self.orientation)) - 1) > 1e-3:
            raise ValueError('orientation must be a unit quaternion')


@dataclass
class XRInput:
    """Continuous controller input state; omitted fields keep their current value."""
    device: str
    buttons: dict = field(default_factory=dict)
    trigger_value: float = None
    squeeze_value: float = None
    thumbstick: list = None
    kind: str = 'xr_input'

    def __post_init__(self):
        if self.device not in XR_HANDS:
            raise ValueError('xr_input device must be left or right')
        if any(name not in XR_BUTTONS or type(value) is not bool for name, value in self.buttons.items()):
            raise ValueError('xr_input buttons map known names to booleans')
        for name in XR_SCALARS:
            value = getattr(self, name)
            if value is not None and (not isinstance(value, (int, float)) or not 0 <= value <= 1):
                raise ValueError(f'{name} must be in 0..1')
        if self.thumbstick is not None:
            _vector(self.thumbstick, 2, 'thumbstick', bound=1)
        if not self.buttons and all(getattr(self, n) is None for n in (*XR_SCALARS, 'thumbstick')):
            raise ValueError('xr_input needs at least one input')


@dataclass
class Wait:
    """Advance game time by milliseconds or game ticks (exactly one)."""
    ms: float = None
    ticks: int = None
    kind: str = 'wait'

    def __post_init__(self):
        if (self.ms is None) == (self.ticks is None):
            raise ValueError('wait takes exactly one of ms or ticks')
        if self.ms is not None and (not isinstance(self.ms, (int, float)) or not 0 < self.ms <= 600_000):
            raise ValueError('wait ms must be in (0, 600000]')
        if self.ticks is not None and (type(self.ticks) is not int or self.ticks <= 0):
            raise ValueError('wait ticks must be a positive integer')


ACTIONS = {cls.kind: cls for cls in (Click, ButtonPress, ButtonRelease, Move, Drag, Scroll, KeyChord, KeyDown,
                                     KeyUp, TypeText, PadButton, PadAxis, XRPose, XRInput, Wait)}
# Device groups a sandbox may declare; checkers and runtimes reject undeclared ones.
DEVICES = {'desktop': ('click', 'button_press', 'button_release', 'move', 'drag', 'scroll', 'key_chord',
                       'key_down', 'key_up', 'type_text'),
           'gamepad': ('pad_button', 'pad_axis'), 'xr': ('xr_pose', 'xr_input'), 'time': ('wait',)}


def action_from_dict(value):
    if not isinstance(value, dict) or value.get('kind') not in ACTIONS:
        raise ValueError(f'unknown action kind: {value.get("kind") if isinstance(value, dict) else value!r}')
    cls = ACTIONS[value['kind']]
    known = {f.name for f in fields(cls)}
    unknown = set(value) - known
    if unknown:
        raise ValueError(f'{value["kind"]} has unknown fields: {sorted(unknown)}')
    return cls(**value)


def action_to_dict(action):
    return {key: value for key, value in asdict(action).items() if value is not None}


@dataclass
class Step:
    """Apply actions in order, then return the requested observations."""
    actions: list = field(default_factory=list)
    observe: list = field(default_factory=lambda: ['frame'])
    id: str = None

    def __post_init__(self):
        self.actions = [a if isinstance(a, tuple(ACTIONS.values())) else action_from_dict(a) for a in self.actions]
        if any(name not in OBSERVATIONS for name in self.observe) or len(set(self.observe)) != len(self.observe):
            raise ValueError(f'observe lists distinct names from {OBSERVATIONS}')
        if not self.actions and not self.observe:
            raise ValueError('a step needs actions or observations')

    def devices(self):
        return {group for group, kinds in DEVICES.items() for a in self.actions if a.kind in kinds}

    def to_dict(self):
        value = {'actions': [action_to_dict(a) for a in self.actions], 'observe': list(self.observe)}
        return {**value, 'id': self.id} if self.id is not None else value

    @classmethod
    def from_dict(cls, value):
        unknown = set(value) - {'actions', 'observe', 'id'}
        if unknown:
            raise ValueError(f'step has unknown fields: {sorted(unknown)}')
        return cls(**value)

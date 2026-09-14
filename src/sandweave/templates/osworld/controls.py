"""Xorg desktop transport using the benchmark's original input implementation."""
import base64
import copy
import io
import json
import shlex
import threading
import time
import uuid

from ...sandbox.asyncio import dualmethod
from ...sandbox.errors import SetupError
from ..gnome.controls import Desktop, DesktopObservation, Keyboard


def decode(value):
    from PIL import Image
    with Image.open(io.BytesIO(value['png'])) as image:
        return image.convert('RGB')


class XorgKeyboard(Keyboard):
    @dualmethod
    def type(self, text):
        return self.desktop.action({'keyboard': {'text': text}})


class XorgDesktop(Desktop):
    def __init__(self, sandbox, config):
        super().__init__(sandbox, config)
        self.keyboard = XorgKeyboard(self)

    @dualmethod
    def screenshot(self, *, fresh=True):
        return decode(self._call('screenshot', fresh=fresh))

    @dualmethod
    def step(self, action):
        value = self._call('step', action=action)
        return DesktopObservation(decode(value), value['metadata'])


class AttachedXorg:
    def __init__(self, context, config, cold):
        self.context, self.config = context, config
        self.lock = threading.RLock()
        self.history = []
        self.user, self.display = config['user'], config['display']
        deadline = time.monotonic() + context.remaining(config.get('ready_timeout', 300))
        while True:
            result = context.run(argv=['pgrep', '-u', self.user, '-x', 'gnome-shell'], check=False, timeout=5)
            if result['returncode'] == 0:
                break
            if time.monotonic() >= deadline:
                raise SetupError('GDM did not start the Ubuntu GNOME session')
            time.sleep(.25)
        self.run_user('xhost +local:')
        if cold:
            time.sleep(context.remaining(config.get('boot_settle', 20)))
        probe = ['/usr/bin/python', '-c', 'import urllib.request; '
                 'urllib.request.urlopen("http://127.0.0.1:5000/platform", timeout=2).read()']
        if context.run(argv=probe, check=False, timeout=5)['returncode']:
            context.run(argv=['systemctl', 'reset-failed', 'osworld.service'])
            context.run(argv=['systemctl', 'restart', 'osworld.service'])
            deadline = time.monotonic() + context.remaining(120)
            while context.run(argv=probe, check=False, timeout=5)['returncode']:
                if time.monotonic() >= deadline:
                    detail = context.run(argv=['journalctl', '-u', 'osworld.service', '-n', '60', '--no-pager'],
                                         check=False)['stdout'].decode(errors='replace')
                    raise SetupError('OSWorld desktop server did not become ready: ' + detail)
                time.sleep(.5)
        size = self.run_user('xdotool getdisplaygeometry')['stdout'].decode().split()
        if list(map(int, size)) != config['resolution']:
            raise SetupError('OSWorld desktop resolution differs from the reference')
        self.capture()

    def run_user(self, command, *, timeout=120):
        return self.context.run(argv=['sudo', '-u', self.user, 'env',
            'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus',
            'HOME=/home/' + self.user, 'USER=' + self.user, 'DISPLAY=' + self.display,
            'XAUTHORITY=/run/user/1000/gdm/Xauthority', 'bash', '-lc', command], timeout=timeout)

    def capture(self):
        path = '/tmp/sandweave-screen-' + uuid.uuid4().hex + '.png'
        try:
            self.run_user('scrot -o -p ' + shlex.quote(path))
            png = self.context.files.read_bytes(path)
        finally:
            self.context.run(argv=['rm', '-f', path], check=False)
        if not png.startswith(b'\x89PNG\r\n\x1a\n'):
            raise RuntimeError('scrot did not produce a PNG')
        return {'png': png, 'metadata': {'captured_ns': time.monotonic_ns(), 'cursor': True,
                                        'resolution': self.config['resolution']}}

    def inject(self, action):
        if isinstance(action, list):
            for value in action:
                self.inject(value)
            return
        if isinstance(action, str) and action in ('FAIL', 'DONE'):
            return  # Recorded for OSWorld's canonical terminal-action evaluator.
        if not isinstance(action, dict):
            raise ValueError('desktop action must be a mapping or list of mappings')
        if action.get('action') == 'wait':
            delay = float(action.get('time', .5))
            if not 0 <= delay <= 120:
                raise ValueError('desktop wait must be between 0 and 120 seconds')
            time.sleep(delay)
            return
        mouse, commands = action.get('mouse') or {}, []
        for key, button, count in (('left_click', 1, 1), ('right_click', 3, 1),
                                   ('middle_click', 2, 1), ('double_click', 1, 2), ('triple_click', 1, 3)):
            if key in mouse:
                x, y = map(int, mouse[key])
                repeat = f' --repeat {count}' if count != 1 else ''
                commands.append(f'mousemove {x} {y} click{repeat} {button}')
        if 'move' in mouse:
            x, y = map(int, mouse['move'])
            commands.append(f'mousemove {x} {y}')
        for name, button in (('left', 1), ('right', 3), ('middle', 2)):
            key = name + '_click_drag'
            if key in mouse:
                points = list(mouse[key])
                if not points:
                    raise ValueError('drag requires coordinates')
                x, y = map(int, points[0])
                command = f'mousemove {x} {y} mousedown {button}' if len(points) == 2 else f'mousedown {button}'
                x, y = map(int, points[1] if len(points) == 2 else points[0])
                commands.append(command + f' mousemove {x} {y} mouseup {button}')
        buttons = mouse.get('buttons') or {}
        if isinstance(buttons, str):
            buttons = {buttons: True}
        for direction in ('down', 'up'):
            for name, button in (('left', 1), ('right', 3), ('middle', 2)):
                if buttons.get(name + '_' + direction):
                    commands.append(f'mouse{direction} {button}')
        if 'scroll' in mouse:
            scroll = mouse['scroll']
            scroll = scroll if isinstance(scroll, dict) else {'dy': scroll}
            for axis, positive, negative in (('dy', 5, 4), ('dx', 7, 6)):
                amount = int(scroll.get(axis, 0))
                if amount:
                    commands.append(f'click {positive if amount > 0 else negative} ' * abs(amount))
        for command in commands:
            self.run_user('xdotool ' + command, timeout=30)
        if action.get('keyboard'):
            encoded = base64.b64encode(json.dumps(action['keyboard'], ensure_ascii=True).encode()).decode()
            script = self.config['keyboard_source'] + '\nimport base64, json\nrun_keyboard(json.loads(base64.b64decode(' + repr(encoded) + ')))\n'
            self.run_user('python3 -c ' + shlex.quote(script))

    def call(self, method, parameters):
        with self.lock:
            if method == 'history':
                return {'steps': copy.deepcopy(self.history)}
            if method in ('action', 'step'):
                action = parameters['action']
                self.inject(action)
                self.history.append({'event': 'step', 'action': copy.deepcopy(action)})
                if self.config.get('action_settle'):
                    time.sleep(self.config['action_settle'])
                if method == 'action':
                    return {'done': False}
            if method in ('screenshot', 'step'):
                return self.capture()
            raise ValueError('unknown Xorg control method: ' + method)

    def detach(self, reason):
        return False


class XorgProvider:
    api_version = 1

    @staticmethod
    def descriptor(config):
        return {'version': 1, 'schema': 'desktop.mouse-keyboard.v1', 'backend': 'xorg',
                'state': ['filesystem'], 'acknowledgement': 'X11 input completion and configured settle delay'}

    @staticmethod
    def bind(sandbox, config):
        return XorgDesktop(sandbox, config)

    @staticmethod
    def attach(context, config, cold=False):
        return AttachedXorg(context, config, cold)

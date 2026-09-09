"""Use the qualified leased SDL state writer; distinguish writes from game ACKs."""
import math

from ...sandbox.asyncio import dualmethod


class Gamepad:
    def __init__(self, sandbox, config):
        self.sandbox = sandbox

    @dualmethod
    def action(self, *, buttons=(), axes=None, hold=.2):
        return self.sandbox._call('control', name='gamepad', method='action',
                                 parameters={'buttons': list(buttons), 'axes': axes or {}, 'hold': hold})

    @dualmethod
    def press(self, button, *, hold=.2):
        return self.action(buttons=[button], hold=hold)


class AttachedGamepad:
    def __init__(self, context, config):
        self.context = context
        self.script = '/opt/sandweave/gamepad-input.py'
        context.file(op='write', path=self.script, data=(context.runtime.root / 'scripts/gamepad-input.py').read_bytes(),
                     truncate=True, mode=0o755)

    def call(self, method, parameters):
        if method != 'action':
            raise ValueError('unknown gamepad operation')
        hold = parameters.get('hold', .2)
        if not isinstance(hold, (float, int)) or not math.isfinite(hold) or not 0 < hold <= 10:
            raise ValueError('gamepad hold must be in (0, 10] seconds')
        command = ['python3', self.script, '--hold', str(hold)]
        for button in parameters.get('buttons', []):
            command += ['--button', str(button)]
        for axis, value in parameters.get('axes', {}).items():
            command += ['--axis', str(axis) + '=' + str(value)]
        result = self.context.run(argv=command, user='ga', timeout=hold+5)
        return {'guest_seconds': (result['finished_ns'] - result['started_ns']) / 1e9,
                'acknowledgement': 'leased input written and released; no SDL consumption or game-tick guarantee'}

    def detach(self, reason):
        return False


class GamepadProvider:
    api_version = 1

    @staticmethod
    def descriptor(config):
        return {'version': 1, 'schema': 'sdl.leased-gamepad.v1', 'state': ['filesystem', 'memory'],
                'acknowledgement': 'leased state file; game consumption requires application evidence'}

    @staticmethod
    def bind(sandbox, config):
        return Gamepad(sandbox, config)

    @staticmethod
    def attach(context, config, cold=False):
        return AttachedGamepad(context, config)

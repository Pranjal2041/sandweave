"""Use the qualified leased SDL state writer; distinguish writes from game ACKs.

The writer runs persistently in the guest (gamepad-input.py --serve): buttons and
axes stay set until changed, renewed under a short lease, so a lost controller
leaves the pad neutral within one second. Timed presses set and release state.
The same writer reports the rumble the game requests through the SDL proxy.
"""
import json
import math
import threading
import time
import uuid

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

    @dualmethod
    def state(self, *, buttons_down=(), buttons_up=(), axes=None):
        """Set persistent state: buttons stay down and axes stay set until changed."""
        return self.sandbox._call('control', name='gamepad', method='state',
                                 parameters={'buttons_down': list(buttons_down), 'buttons_up': list(buttons_up),
                                             'axes': axes or {}})

    @dualmethod
    def haptics(self, *, after=None):
        """Rumble the game requested since the previous call (SDL_JoystickRumble/RumbleTriggers).

        Each event has output ('motors' or 'triggers'), guest_ns, the two strengths in 0..1
        and duration_ms; `after` re-reads from a record index instead.
        """
        return self.sandbox._call('control', name='gamepad', method='haptics', parameters={'after': after})


class AttachedGamepad:
    def __init__(self, context, config):
        self.context = context
        self.script = '/opt/sandweave/gamepad-input.py'
        context.file(op='write', path=self.script, data=(context.runtime.root / 'scripts/gamepad-input.py').read_bytes(),
                     truncate=True, mode=0o755)
        self.process_id, self.lock, self.buffer, self.offset = uuid.uuid4().hex, threading.Lock(), b'', 0
        self.haptics_cursor = 0
        context.worker.command_start(context.id, self.process_id, argv=['python3', '-u', self.script, '--serve'],
                                     user='ga')
        if not self._reply(10).get('ready'):
            raise RuntimeError('gamepad writer did not start')

    def _reply(self, timeout=5):
        deadline = time.monotonic() + timeout
        while b'\n' not in self.buffer:
            state = self.context.worker.process_wait(self.context.id, self.process_id, timeout=min(10, max(
                .01, deadline - time.monotonic())), stream='stdout', offset=self.offset)
            if state.get('stdout_size', 0) > self.offset:
                data = self.context.worker.process_output(self.context.id, self.process_id, stream='stdout',
                                                          offset=self.offset, size=65536)
                self.buffer += data
                self.offset += len(data)
            if state.get('returncode') is not None and b'\n' not in self.buffer:
                error = self.context.worker.process_output(self.context.id, self.process_id, stream='stderr', size=4096)
                raise RuntimeError('gamepad writer exited: ' + error.decode(errors='replace'))
            if time.monotonic() >= deadline and b'\n' not in self.buffer:
                raise TimeoutError('gamepad writer did not acknowledge; delivery outcome unknown')
        line, _, self.buffer = self.buffer.partition(b'\n')
        reply = json.loads(line)
        if 'error' in reply:
            raise ValueError(reply['error'])
        return reply

    def _update(self, update):
        with self.lock:
            sent = time.monotonic_ns()
            self.context.worker.process_stdin(self.context.id, self.process_id, data=(json.dumps(update) + '\n').encode())
            reply = self._reply()
            return {**reply, 'host_sent_ns': sent, 'host_ack_ns': time.monotonic_ns(),
                    'acknowledgement': 'leased state written; no SDL consumption or game-tick guarantee'}

    def call(self, method, parameters):
        if method == 'haptics':
            after = parameters.get('after')
            with self.lock:
                self.context.worker.process_stdin(self.context.id, self.process_id, data=(json.dumps(
                    {'haptics_after': self.haptics_cursor if after is None else after}) + '\n').encode())
                value = self._reply()
                if after is None:
                    self.haptics_cursor = value['next']
            return value
        if method == 'state':
            return self._update({key: parameters.get(key, default) for key, default in
                                 (('buttons_down', []), ('buttons_up', []), ('axes', {}))})
        if method != 'action':
            raise ValueError('unknown gamepad operation')
        hold = parameters.get('hold', .2)
        if not isinstance(hold, (float, int)) or not math.isfinite(hold) or not 0 < hold <= 10:
            raise ValueError('gamepad hold must be in (0, 10] seconds')
        buttons, axes = parameters.get('buttons', []), parameters.get('axes', {})
        started = time.monotonic_ns()
        self._update({'buttons_down': buttons, 'axes': axes})
        time.sleep(hold)
        released = self._update({'buttons_up': buttons, 'axes': {name: 0 for name in axes}})
        return {**released, 'guest_seconds': (time.monotonic_ns() - started) / 1e9}

    def detach(self, reason):
        try:
            self.context.worker.process_stdin(self.context.id, self.process_id, close=True)
        except Exception:
            pass
        return False


class GamepadProvider:
    api_version = 1

    @staticmethod
    def descriptor(config):
        return {'version': 2, 'schema': 'sdl.leased-gamepad.v2', 'state': ['filesystem', 'memory'],
                'persistent_state': 'buttons and axes hold until changed; neutral when detached or the writer stops',
                'acknowledgement': 'leased state file; game consumption requires application evidence'}

    @staticmethod
    def bind(sandbox, config):
        return Gamepad(sandbox, config)

    @staticmethod
    def attach(context, config, cold=False):
        return AttachedGamepad(context, config)

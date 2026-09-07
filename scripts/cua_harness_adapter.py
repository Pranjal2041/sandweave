"""cua-auto-harness binding for the standalone gVisor Xvnc fast I/O API."""
from pathlib import Path
import shlex
import subprocess
import sys
import time
import uuid

from autoharness import Adapter, register_adapter
from autoharness.adapters import SutExecutionError, UnsupportedActionError
from autoharness.adapters.gym_anything import build_xdotool_commands, XDOTOOL_KEYSYM
from autoharness.contracts import ButtonPress, ButtonRelease, Click, Drag, KeyChord, Move, Scroll, TypeText, Wait

sys.path.insert(0, str(Path(__file__).resolve().parent))
from environment import EnvironmentManager
from fast_io import events_for_action


def translate(action):
    """Translate vocabulary only; every emitted input goes through FastIOClient."""
    def wrap(modifiers, body):
        if not modifiers:
            return body
        return ([{'keyboard': {'keys_down': modifiers}}] + body +
                [{'keyboard': {'keys_up': modifiers}}])

    if isinstance(action, Click):
        names = {(1,'left'):'left_click', (1,'middle'):'middle_click', (1,'right'):'right_click',
                 (2,'left'):'double_click', (3,'left'):'triple_click'}
        kind = names.get((action.count, action.button))
        if kind is None:
            raise UnsupportedActionError(f'fast I/O has no {action.count}x {action.button} click')
        return wrap(action.modifiers, [{'mouse': {kind: [action.x, action.y]}}])
    if isinstance(action, (ButtonPress, ButtonRelease)):
        if action.button not in ('left','middle','right'):
            raise UnsupportedActionError('fast I/O has no ' + action.button + ' button state')
        direction = 'down' if isinstance(action, ButtonPress) else 'up'
        return [{'mouse': {'move': [action.x, action.y], 'buttons': action.button + '_' + direction}}]
    if isinstance(action, Move):
        return [{'mouse': {'move': [action.x, action.y]}}]
    if isinstance(action, Drag):
        if action.button not in ('left','middle','right'):
            raise UnsupportedActionError('fast I/O has no ' + action.button + ' drag')
        if action.button in ('left','right'):
            core = [{'mouse': {action.button + '_click_drag': [list(p) for p in action.path]}}]
        else:
            # The public API has middle-button states, but no named middle drag.
            core = [{'mouse': {'move': list(action.path[0]), 'buttons': 'middle_down'}}]
            for start, end in zip(action.path, action.path[1:]):
                for step in range(1,9):
                    point = [round(start[d]+(end[d]-start[d])*step/8) for d in (0,1)]
                    core.append({'mouse': {'move': point}})
            core.append({'mouse': {'buttons': 'middle_up'}})
        return wrap(action.modifiers, core)
    if isinstance(action, Scroll):
        if action.dx_ticks:
            raise UnsupportedActionError('fast I/O has no horizontal-scroll action')
        return wrap(action.modifiers, [{'mouse': {'move': [action.x, action.y], 'scroll': action.dy_ticks}}])
    if isinstance(action, KeyChord):
        # Canonical aliases to X keysyms; the public API already accepts these.
        return [{'keyboard': {'keys': [XDOTOOL_KEYSYM.get(k, k) for k in action.keys]}}]
    if isinstance(action, TypeText):
        return [{'keyboard': {'text': action.text}}]
    if isinstance(action, Wait):
        return []
    raise UnsupportedActionError('unrecognized canonical action: ' + repr(action))


@register_adapter('gvisor_xvnc')
class GVisorXvncAdapter(Adapter):
    guest_display = ':1'
    supports_raw_tap = True
    runners = ('fastio', 'xdotool-reference')
    default_runner = 'fastio'
    options = ('name', 'keep', 'gpu')

    def __init__(self, env_dir=None, runner='fastio', options=None):
        options = options or {}
        if runner not in self.runners:
            raise ValueError('runner must be fastio or xdotool-reference')
        self.manager = EnvironmentManager()
        self.name = options.get('name') or 'fastio-harness-' + uuid.uuid4().hex[:8]
        if not self.name.startswith('fastio-harness-'):
            raise ValueError('use a disposable name starting with fastio-harness-')
        self.keep = bool(options.get('keep', False))
        self.gpu = options.get('gpu')
        self.runner = runner
        self.client = None
        self.owned = False

    def boot(self):
        if self.manager.status(self.name)['status'] != 'missing':
            raise ValueError('choose a new disposable environment name')
        flags = ['--guest-gs', '--no-runtime-debug', '--memory-mib', '8192', '--runtime-memory-mib', '1024']
        if self.gpu is not None:
            flags += ['--gpu', str(self.gpu)]
        self.owned = True
        try:
            self.manager.start(self.name, options=flags)
            deadline = time.monotonic() + 90
            while self.exec('systemctl is-active tigervncserver@:1.service && '
                           'xprop -root _NET_SUPPORTING_WM_CHECK | '
                           'grep -q "window id # 0x"', timeout=10)[0]:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Xvnc did not become ready')
                time.sleep(.5)
            session_ready = ('runuser -u ga -- env DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus '
                             'gdbus call --session --dest org.gnome.SessionManager '
                             '--object-path /org/gnome/SessionManager '
                             '--method org.gnome.SessionManager.IsSessionRunning')
            while self.exec(session_ready, timeout=10)[1].strip() != '(true,)':
                if time.monotonic() >= deadline:
                    raise TimeoutError('GNOME session did not finish starting')
                time.sleep(.5)
            rc, output = self.exec('xrandr --output VNC-0 --mode 1920x1080')
            if rc:
                raise RuntimeError(output)
            # GNOME applies its monitor configuration after Xvnc starts. Do
            # not launch the fullscreen probe until that desktop is stable.
            stable = time.monotonic()
            while time.monotonic() - stable < 2:
                rc, output = self.exec('xrandr --current')
                if rc or 'current 1920 x 1080' not in output:
                    self.exec('xrandr --output VNC-0 --mode 1920x1080')
                    stable = time.monotonic()
                if time.monotonic() >= deadline:
                    raise TimeoutError('desktop resolution did not stabilize')
                time.sleep(.25)
            # These are the harness's observation and between-rep cleanup tools.
            rc, output = self.exec('python3 -c "import tkinter, Xlib" && '
                                   'command -v xdotool && command -v xset && command -v curl')
            if rc:
                rc, output = self.exec('DEBIAN_FRONTEND=noninteractive apt-get update -qq && '
                    'DEBIAN_FRONTEND=noninteractive apt-get install -y python3-tk python3-xlib xdotool x11-xserver-utils curl', timeout=300)
                if rc:
                    raise RuntimeError(output[-4000:])
            self.client = self.manager.fast_io(self.name)
            while True:
                image = self.client.screenshot()
                bounds = image.convert('L').getbbox()
                if image.size == (1920,1080) and bounds and bounds[2] > 1440 and bounds[3] > 810:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('GNOME has not painted the desktop')
                time.sleep(.25)
            self.exec('xdotool key Escape')
        except BaseException:
            self.teardown()
            raise

    def exec(self, cmd, timeout=120):
        command = [*self.manager._command(self.name), 'exec', '--env=DISPLAY=:1',
                   '--env=XAUTHORITY=/home/ga/.Xauthority', self.name, 'sh', '-c', cmd]
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return result.returncode, result.stdout + result.stderr

    def copy_in(self, local_path, guest_path):
        command = [*self.manager._command(self.name), 'exec', self.name, 'sh', '-c',
                   'cat > ' + shlex.quote(guest_path)]
        result = subprocess.run(command, input=Path(local_path).read_bytes(), capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors='replace'))

    def supports(self, action):
        try:
            if self.runner == 'fastio':
                events_for_action(translate(action))
            elif not isinstance(action, Wait):
                build_xdotool_commands(action)
        except (UnsupportedActionError, ValueError) as error:
            return str(error)
        return None

    def inject(self, action):
        if isinstance(action, Wait):
            time.sleep(action.seconds)
            return
        try:
            if self.runner == 'xdotool-reference':
                for arguments in build_xdotool_commands(action):
                    rc, output = self.exec(shlex.join(['xdotool', *arguments]))
                    if rc:
                        raise RuntimeError(output)
            else:
                # Exercise the public gesture implementation as-is. Its drags
                # have no duration option; do not insert adapter-only delays.
                self.client.action(translate(action))
        except UnsupportedActionError:
            raise
        except Exception as error:
            raise SutExecutionError(f'{type(error).__name__}: {error}') from error

    def screenshot(self, dst):
        self.client.screenshot().save(dst)

    def teardown(self):
        if self.client:
            self.client.close()
            self.client = None
        if self.owned and not self.keep:
            self.manager.stop(self.name, discard=True)
            self.owned = False

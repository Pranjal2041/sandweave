"""Desktop controls own display readiness, input semantics and observations."""
from dataclasses import dataclass
import time

from ...sandbox.asyncio import dualmethod
from ...sandbox.errors import UnsupportedFeature
from ...sandbox.timings import measure


@dataclass(frozen=True)
class DesktopObservation:
    image: object
    metadata: dict


def vnc_password(sandbox):
    """Read the installed credential, checking it against TigerVNC's file.

    Older prepared images lack the plaintext file and used the lab password.
    Never advertise that fallback unless its encrypted bytes still match.
    Runs through the command API so existing workers can be inspected too.
    """
    result = sandbox.run(argv=['python3', '-c', '''
from pathlib import Path
import subprocess
try:
    encrypted = Path('/home/ga/.vnc/passwd').read_bytes()[:8]
    source = Path('/etc/sandweave-vnc-password')
    password = source.read_bytes().splitlines()[0][:8] if source.exists() else b'labvnc01'
    generated = subprocess.run(['tigervncpasswd', '-f'], input=password + b'\\n',
                               capture_output=True, check=True, timeout=2).stdout
    if len(encrypted) == 8 and generated[:8] == encrypted:
        print(password.decode('utf-8'), end='')
except (OSError, IndexError, UnicodeError, subprocess.SubprocessError):
    pass
'''], user='root', timeout=5, check=False)
    return result.stdout if result.returncode == 0 and result.stdout else None


def decode_image(value):
    from PIL import Image
    return Image.frombytes('RGB', tuple(value['size']), value['rgb'])


class Mouse:
    def __init__(self, desktop):
        self.desktop = desktop

    @dualmethod
    def click(self, x, y, *, button='left'):
        return self.desktop.action({'mouse': {button + '_click': [x, y]}})

    @dualmethod
    def move(self, x, y):
        return self.desktop.action({'mouse': {'move': [x, y]}})

    @dualmethod
    def scroll(self, dy=0, *, dx=0):
        return self.desktop.action({'mouse': {'scroll': {'dx': dx, 'dy': dy}}})

    @dualmethod
    def drag(self, points, *, button='left'):
        return self.desktop.action({'mouse': {button + '_click_drag': points}})


class Keyboard:
    def __init__(self, desktop):
        self.desktop = desktop

    @dualmethod
    def type(self, text):
        # The underlying input server has explicit per-batch event limits.
        result = None
        for offset in range(0, len(text), 1024):
            result = self.desktop.action({'keyboard': {'text': text[offset:offset+1024]}})
        return result

    @dualmethod
    def press(self, keys):
        return self.desktop.action({'keyboard': {'keys': keys}})

    @dualmethod
    def down(self, keys):
        return self.desktop.action({'keyboard': {'keys_down': keys}})

    @dualmethod
    def up(self, keys):
        return self.desktop.action({'keyboard': {'keys_up': keys}})


class Desktop:
    def __init__(self, sandbox, config):
        self.sandbox, self.config = sandbox, config
        self.mouse, self.keyboard = Mouse(self), Keyboard(self)

    def _call(self, method, **parameters):
        return self.sandbox._call('control', name='desktop', method=method, parameters=parameters)

    @dualmethod
    def screenshot(self, *, fresh=True):
        return decode_image(self._call('screenshot', fresh=fresh))

    @dualmethod
    def action(self, action):
        return self._call('action', action=action)

    @dualmethod
    def step(self, action):
        value = self._call('step', action=action)
        return DesktopObservation(decode_image(value), value['metadata'])


class AttachedDesktop:
    def __init__(self, context, config, cold):
        backend = config.get('backend', 'xvnc')
        if backend != 'xvnc':
            raise UnsupportedFeature('desktop fast I/O currently supports xvnc; Wayland is an explicit lab option')
        self.client = context.runtime.manager.fast_io(context.id, backend=backend)
        deadline = time.monotonic() + context.remaining(config.get('ready_timeout', 120))
        try:
            with measure('desktop_display_seconds'):
                self._wait_for_display(context, deadline)
            with measure('desktop_session_seconds'):
                self._wait_for_session(context, deadline)
            with measure('desktop_startup_seconds'):
                if cold:
                    self._finish_startup(context, config, deadline)
            # Keep monitor changes out of Shell's startup animation.
            with measure('desktop_configure_seconds'):
                if cold and config.get('resolution'):
                    width, height = config['resolution']
                    if type(width) is not int or type(height) is not int or not (320 <= width <= 3840 and 240 <= height <= 2160):
                        raise ValueError('desktop resolution must be between 320x240 and 3840x2160')
                    context.run(argv=['xrandr', '--output', 'VNC-0', '--mode', f'{width}x{height}'], user='ga',
                                env={'DISPLAY': ':1', 'XAUTHORITY': '/home/ga/.Xauthority'})
            with measure('desktop_paint_seconds'):
                if config.get('wait_for_paint', True):
                    self._wait_for_paint(deadline)
        except BaseException:
            self.client.close()
            raise

    def _wait_for_display(self, context, deadline):
        while True:
            try:
                window_manager = context.run(argv=['xprop', '-root', '_NET_SUPPORTING_WM_CHECK', '_NET_CLIENT_LIST'],
                    user='ga', env={'DISPLAY': ':1', 'XAUTHORITY': '/home/ga/.Xauthority'}, timeout=5, check=False)
                if b'window id # 0x' in window_manager['stdout'] and b'_NET_CLIENT_LIST(WINDOW)' in window_manager['stdout']:
                    self.client.screenshot()
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('GNOME window manager did not become ready')
            except Exception:
                if time.monotonic() >= deadline:
                    raise
            time.sleep(.2)

    def _finish_startup(self, context, config, deadline):
        # IsSessionRunning precedes Shell's startup animation and initial
        # overview. Wait for Shell's own startup-complete notification before
        # changing presentation, or its later animation can undo the change.
        while True:
            started = context.run(argv=['journalctl', '--boot', '--quiet', '--no-pager', '--output=cat', '--lines=1',
                'MESSAGE_ID=f3ea493c22934e26811cd62abe8e203a', '_UID=1000'], timeout=5, check=False)
            if started['returncode'] == 0 and started['stdout'].strip():
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('GNOME Shell did not finish its startup animation')
            time.sleep(.1)
        if config.get('initial_overview', False):
            return
        bus = ['gdbus', 'call', '--session', '--dest', 'org.gnome.Shell',
               '--object-path', '/org/gnome/Shell', '--method']
        environment = {'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1000/bus'}
        context.run(argv=[*bus, 'org.freedesktop.DBus.Properties.Set',
                         'org.gnome.Shell', 'OverviewActive', '<false>'],
                    user='ga', env=environment, timeout=5)
        while True:
            state = context.run(argv=[*bus, 'org.freedesktop.DBus.Properties.Get',
                                      'org.gnome.Shell', 'OverviewActive'],
                                user='ga', env=environment, timeout=5)
            if state['stdout'].strip() == b'(<false>,)':
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('GNOME overview did not close before the readiness deadline')
            time.sleep(.1)

    def _wait_for_session(self, context, deadline):
        while True:
            session = context.run(argv=['gdbus', 'call', '--session', '--dest', 'org.gnome.SessionManager',
                '--object-path', '/org/gnome/SessionManager', '--method', 'org.gnome.SessionManager.IsSessionRunning'],
                user='ga', env={'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1000/bus'}, timeout=5, check=False)
            if session['stdout'].strip() == b'(true,)':
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('GNOME session did not finish starting')
            time.sleep(.2)

    def _wait_for_paint(self, deadline):
        while True:
            image = self.client.screenshot()
            bounds = image.convert('L').getbbox()
            if bounds and bounds[2] > image.width * .75 and bounds[3] > image.height * .75:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError('GNOME did not paint its desktop before the readiness deadline')
            time.sleep(.1)

    def call(self, method, parameters):
        if method == 'action':
            return self.client.action(**parameters)
        if method == 'screenshot':
            image = self.client.screenshot(**parameters)
            metadata = self.client.last_metadata
        elif method == 'step':
            image, metadata = self.client.step(**parameters)
        else:
            raise UnsupportedFeature('unknown desktop operation: ' + method)
        return {'size': list(image.size), 'rgb': image.tobytes(), 'metadata': metadata}

    def detach(self, reason):
        self.client.close()


class DesktopProvider:
    api_version = 1

    @staticmethod
    def descriptor(config):
        return {'version': 1, 'schema': 'desktop.mouse-keyboard.v1', 'backend': config.get('backend', 'xvnc'),
                'state': ['filesystem', 'memory'], 'acknowledgement': 'input-server fence; no application repaint guarantee'}

    @staticmethod
    def bind(sandbox, config):
        return Desktop(sandbox, config)

    @staticmethod
    def attach(context, config, cold=False):
        return AttachedDesktop(context, config, cold)

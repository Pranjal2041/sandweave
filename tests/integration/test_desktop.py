"""Inspect actual desktop output and exercise keyboard input against an application."""
import os
from pathlib import Path
import time
import uuid

import pytest

from sandweave import Sandbox
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit worker required')]


@pytest.fixture(scope='module', autouse=True)
def release_idle_test_worker():
    yield
    connection = local_connection()
    connection.call('_shutdown_if_idle')
    connection.close()


def test_desktop_actions_pause_and_filesystem_restore():
    artifacts = Path(os.environ['SANDWEAVE_ASSETS']) / 'runs/sdk-acceptance/desktop'
    artifacts.mkdir(parents=True, exist_ok=True)
    with Sandbox(template='gnome') as env:
        image = env.desktop.screenshot()
        assert image.mode == 'RGB' and image.size == (1280, 800)
        image.save(artifacts / 'initial.png')
        env.desktop.keyboard.press('Escape')
        time.sleep(.3)  # Leave GNOME's initial overview before focusing the test app.
        source = '''import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
from pathlib import Path
window = Gtk.Window(title="Sandweave desktop acceptance")
window.set_default_size(760, 220)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
box.set_border_width(28)
box.pack_start(Gtk.Label(label="Type through the sandbox desktop API"), False, False, 0)
entry = Gtk.Entry()
entry.connect("changed", lambda widget: Path("/workspace/typed").write_text(widget.get_text()))
box.pack_start(entry, False, False, 0)
window.add(box)
window.connect("destroy", Gtk.main_quit)
window.show_all()
entry.grab_focus()
Gtk.main()
'''
        env.files.write_text('/workspace/desktop-test.py', source)
        application = env.exec('python /workspace/desktop-test.py')
        env.run("xdotool search --sync --name 'Sandweave desktop acceptance' windowactivate --sync", timeout=15)
        focused = env.run("xdotool search --name 'Sandweave desktop acceptance' windowfocus --sync", timeout=10, check=False)
        env.desktop.screenshot().save(artifacts / 'before-input.png')
        (artifacts / 'focus.txt').write_text(env.run('xdotool getwindowfocus getwindowname; xprop -root _NET_ACTIVE_WINDOW').stdout)
        assert focused.returncode == 0, focused.stderr
        expected = 'Sandweave: hello € 日本語'
        env.desktop.keyboard.type(expected)
        deadline = time.monotonic() + 5
        while True:
            try:
                actual = env.files.read_text('/workspace/typed')
                if actual == expected:
                    break
            except FileNotFoundError:
                pass
            assert time.monotonic() < deadline, f'application input differs: {actual if "actual" in locals() else "missing"!r}'
            time.sleep(.05)
        observation = env.desktop.step({'mouse': {'move': [900, 650]}})
        # Input acknowledgement does not promise a completed application paint.
        time.sleep(.3)
        observation = env.desktop.step({'mouse': {'move': [900, 650]}})
        observation.image.save(artifacts / 'typed.png')
        assert observation.metadata['frame_input_sequence'] >= observation.metadata['input_sequence']
        env.pause(); env.resume()
        assert env.files.read_text('/workspace/typed') == expected
        env.desktop.screenshot().save(artifacts / 'resumed.png')
        baseline = env.cache('sdk-desktop-' + uuid.uuid4().hex)
        application.terminate()
    with Sandbox(cache=baseline) as restored:
        assert restored.files.read_text('/workspace/typed') == expected
        restored.desktop.screenshot().save(artifacts / 'restored.png')
        assert restored.capabilities['desktop']['version'] == 1

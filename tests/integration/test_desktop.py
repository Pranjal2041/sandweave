"""Inspect actual desktop output and exercise keyboard input against an application."""
import os
from pathlib import Path
import time
import uuid

import pytest

from sandweave import Sandbox, Template
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
    artifacts = Path(os.environ.get('SANDWEAVE_TEST_ARTIFACTS',
                                   str(Path(os.environ['SANDWEAVE_ASSETS']) / 'runs/sdk-acceptance/desktop')))
    artifacts.mkdir(parents=True, exist_ok=True)
    with Sandbox(template='gnome') as env:
        image = env.desktop.screenshot()
        assert image.mode == 'RGB' and image.size == (1920, 1080)
        image.save(artifacts / 'initial.png')
        assert_clean_start(env)
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
        assert_clean_start(restored)
        assert restored.files.read_text('/workspace/typed') == expected
        image = restored.desktop.screenshot()
        assert image.size == (1920, 1080)
        image.save(artifacts / 'restored.png')
        assert restored.capabilities['desktop']['version'] == 1


def overview(env, value=None):
    command = ('gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell '
               '--method org.freedesktop.DBus.Properties.')
    if value is not None:
        return env.run(command + 'Set org.gnome.Shell OverviewActive "<' + value + '>"').stdout
    return env.run(command + 'Get org.gnome.Shell OverviewActive').stdout.strip()


def assert_clean_start(env):
    assert overview(env) == '(<false>,)'
    visible = env.run("xdotool search --onlyvisible --class '^Vncconfig$'", check=False)
    assert visible.returncode == 1 and not visible.stdout
    assert env.run('pgrep -x tigervncconfig', check=False).returncode == 0
    assert env.run('systemctl is-active accounts-daemon', user='root').stdout.strip() == 'active'
    assert env.timings['controls_seconds'] >= env.timings['desktop_startup_seconds'] >= 0


def test_pause_preserves_user_overview_and_startup_timings():
    template = Template({'extends': 'gnome', 'capabilities': {'desktop': {'resolution': [1280, 800]}}})
    with Sandbox(template=template) as env:
        assert_clean_start(env)
        assert env.desktop.screenshot().size == (1280, 800)
        initial_timings = env.timings
        overview(env, 'true')
        deadline = time.monotonic() + 5
        while overview(env) != '(<true>,)':
            assert time.monotonic() < deadline
            time.sleep(.05)
        env.pause()
        env.resume()
        assert overview(env) == '(<true>,)'
        assert env.timings == initial_timings

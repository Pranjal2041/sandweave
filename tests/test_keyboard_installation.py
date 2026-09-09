import importlib

import pytest

from sandweave.installation import needs_helpers
from sandweave.sandbox import workspace
from sandweave.templates.resolve import Template


def test_keyboard_input_uses_installed_definitions_without_host_x11(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(workspace.engine_sources()))
    fast_io = importlib.import_module('fast_io')
    headers = tmp_path / 'tools/helpers/usr/include/X11'
    headers.mkdir(parents=True)
    (headers / 'keysymdef.h').write_text(
        '#define XK_Control_L 0xffe3\n#define XK_Return 0xff0d\n'
        '#define XK_F12 0xffc9\n#define XK_Ydiaeresis 0x13be\n')
    (headers / 'XF86keysym.h').write_text(
        '#define XF86XK_AudioMute 0x1008ff12\n'
        '#define XF86XK_BrightnessAuto _EVDEVK(0x0f4)\n')
    (headers / 'HPkeysym.h').write_text(
        '#define hpXK_Ydiaeresis 0x100000ee\n#define XK_Ydiaeresis 0x100000ee\n')
    monkeypatch.setattr(fast_io, '__file__', str(tmp_path / 'scripts/fast_io.py'))
    monkeypatch.setattr(fast_io, '_key_names', None)
    monkeypatch.setattr(fast_io, '_x11', None)
    monkeypatch.setattr(fast_io.ctypes.util, 'find_library', lambda name: None)
    monkeypatch.setattr(fast_io.ctypes, 'CDLL', lambda *a: pytest.fail('host X11 dependency'))
    assert fast_io.events_for_action({'keyboard': {'keys': ['ctrl', 'enter']}}) == [
        (4, 0xffe3, 0, 0), (4, 0xff0d, 0, 0), (5, 0xff0d, 0, 0), (5, 0xffe3, 0, 0)]
    assert fast_io.keysym('f12') == 0xffc9
    assert fast_io.keysym('XF86AudioMute') == 0x1008ff12
    assert fast_io.keysym('XF86BrightnessAuto') == 0x100810f4
    assert fast_io.keysym('Ydiaeresis') == 0x13be
    assert fast_io.keysym('hpYdiaeresis') == 0x100000ee
    assert fast_io.keysym('中') == 0x01004e2d
    with pytest.raises(ValueError, match='unknown key'):
        fast_io.keysym('not-a-key')


def test_desktop_import_installs_key_definitions_even_with_host_network_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, 'tool', lambda name: '/host/' + name)
    assert not needs_helpers(tmp_path, Template('coding').resolve())
    assert needs_helpers(tmp_path, Template('gnome').resolve())
    headers = tmp_path / 'tools/helpers/usr/include/X11'
    headers.mkdir(parents=True)
    for name in ('keysymdef.h', 'XF86keysym.h'):
        (headers / name).write_text('keyboard definitions')
    assert not needs_helpers(tmp_path, Template('gnome').resolve())
